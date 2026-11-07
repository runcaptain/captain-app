"""Bounded artifact publication with local or private S3 storage."""
from __future__ import annotations

import hashlib
import os
import uuid
import re
import logging
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Table, UniqueConstraint, and_, delete, insert, select, update

from .server import (commit_record, digest, metadata, prior_receipt, receipts,
                    records, visible, require_history)
from .migrations import locked_transaction

MAX_BYTES = 8 * 1024 * 1024
uploads = Table('sync_artifact_uploads', metadata,
    Column('id', String(32), primary_key=True),
    Column('workspace', String(100), nullable=False),
    Column('owner_id', String(100), nullable=False),
    Column('artifact_id', String(100), nullable=False),
    Column('sha256', String(64), nullable=False),
    Column('size', Integer, nullable=False),
    Column('visibility', String(10), nullable=False),
    Column('state', String(12), nullable=False),
    Column('staged_file', String(100)),
    Column('history_id', String(32)),
    UniqueConstraint('workspace', 'artifact_id'))


class Begin(BaseModel):
    history_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    mutation_id: str = Field(min_length=1, max_length=100)
    artifact_id: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=0, le=MAX_BYTES)
    visibility: Literal['private', 'workspace'] = 'private'


class Finalize(BaseModel):
    history_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    mutation_id: str = Field(min_length=1, max_length=100)


class S3ArtifactStore:
    """Private objects only. Authorization remains on the API, never in a URL.

    Staged objects survive instance changes. Publication is conditional and
    content-addressed within a workspace. Keep staging for DB crash retries;
    operators may expire only the staging/ prefix with a lifecycle policy.
    """

    def __init__(self, bucket, prefix='team-sync', *, client=None):
        import boto3
        if not bucket or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise ValueError('invalid_artifact_bucket')
        prefix = prefix.strip('/')
        if not prefix or not re.fullmatch(r'[A-Za-z0-9_/-]+', prefix):
            raise ValueError('invalid_artifact_prefix')
        self.bucket, self.prefix = bucket, prefix
        self.client = client if client is not None else boto3.client('s3')

    def key(self, workspace, name, *, staged=False):
        pattern = r'[0-9a-f]{32}\.upload' if staged else r'[0-9a-f]{64}'
        if not re.fullmatch(pattern, name):
            raise HTTPException(409, 'artifact_unavailable')
        scope = hashlib.sha256(workspace.encode()).hexdigest()
        return f'{self.prefix}/{"staging" if staged else "blobs"}/{scope}/{name}'

    def read(self, key, *, missing='artifact_unavailable'):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
            stream = result['Body']
            try:
                if result['ContentLength'] > MAX_BYTES:
                    raise HTTPException(413, 'artifact_too_large')
                data = stream.read(MAX_BYTES + 1)
            finally:
                stream.close()
            if len(data) > MAX_BYTES:
                raise HTTPException(413, 'artifact_too_large')
            if len(data) != result['ContentLength']:
                raise HTTPException(409, 'stored_artifact_integrity_failure')
            return data
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                raise HTTPException(409, missing) from None
            raise HTTPException(503, 'artifact_storage_unavailable') from None
        except BotoCoreError:
            raise HTTPException(503, 'artifact_storage_unavailable') from None

    def put(self, key, data):
        from botocore.exceptions import BotoCoreError, ClientError
        if len(data) > MAX_BYTES:
            raise HTTPException(413, 'artifact_too_large')
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                ContentType='application/octet-stream', ServerSideEncryption='AES256',
                IfNoneMatch='*')
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') in ('PreconditionFailed', '412'):
                if self.read(key) != data:
                    raise HTTPException(409, 'stored_artifact_integrity_failure') from None
                return
            raise HTTPException(503, 'artifact_storage_unavailable') from None
        except BotoCoreError:
            raise HTTPException(503, 'artifact_storage_unavailable') from None

    def stage(self, workspace, filename, path):
        self.put(self.key(workspace, filename, staged=True), path.read_bytes())

    def delete_stage(self, workspace, filename):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.key(workspace, filename, staged=True))
        except (BotoCoreError, ClientError):
            raise HTTPException(503, 'artifact_storage_unavailable') from None

    def publish(self, workspace, filename, size, sha):
        data = self.read(self.key(workspace, filename, staged=True), missing='artifact_not_ready')
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise HTTPException(422, 'artifact_integrity_mismatch')
        self.put(self.key(workspace, sha), data)

    def download(self, workspace, size, sha):
        data = self.read(self.key(workspace, sha))
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise HTTPException(409, 'stored_artifact_integrity_failure')
        return Response(data, media_type='application/octet-stream', headers={
            'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="artifact.bin"'})


def register_artifacts(app, engine, actor, access, root, *, store=None):
    root = Path(root).resolve()
    staging = root / 'staging'
    blobs = root / 'blobs'
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    blobs.mkdir(parents=True, exist_ok=True, mode=0o700)

    def editor(role):
        if role not in ('editor', 'admin'):
            raise HTTPException(403, 'write_denied')

    def stage(db, workspace, upload_id, user):
        row = db.execute(select(uploads).where(and_(uploads.c.workspace == workspace,
            uploads.c.id == upload_id, uploads.c.owner_id == user))).mappings().first()
        if not row:
            raise HTTPException(404, 'upload_not_found')
        return row

    def current_upload(row, workspace):
        if row['history_id'] != workspace['history_id']:
            raise HTTPException(409, 'stale_upload_history')

    def hash_file(path):
        hasher = hashlib.sha256()
        total = 0
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413, 'artifact_too_large')
                hasher.update(chunk)
        return total, hasher.hexdigest()

    def blob_path(workspace, sha):
        # Neither workspace IDs nor artifact IDs become filesystem path segments.
        directory = blobs / hashlib.sha256(workspace.encode()).hexdigest()
        directory.mkdir(exist_ok=True, mode=0o700)
        return directory / sha

    def verified_stage(workspace, row):
        if row['state'] != 'uploaded' or not row['staged_file']:
            return False
        if store is not None:
            data = store.read(store.key(workspace, row['staged_file'], staged=True), missing='artifact_not_ready')
            actual = len(data), hashlib.sha256(data).hexdigest()
        else:
            try:
                actual = hash_file(staging / row['staged_file'])
            except FileNotFoundError:
                raise HTTPException(409, 'artifact_not_ready') from None
        if actual != (row['size'], row['sha256']):
            raise HTTPException(422, 'artifact_integrity_mismatch')
        return True

    @app.post('/v1/workspaces/{workspace}/artifacts/uploads')
    def begin(workspace: str, change: Begin, authorization: str = Header(default='')):
        request = {'operation':'begin_artifact', **change.model_dump(exclude_none=True)}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            require_history(w, change.history_id)
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            exists = db.execute(select(records.c.id).where(and_(records.c.workspace == workspace,
                records.c.id == change.artifact_id))).first()
            pending = db.execute(select(uploads).where(and_(uploads.c.workspace == workspace,
                uploads.c.artifact_id == change.artifact_id))).mappings().first()
            if not exists and pending and pending['history_id'] != w['history_id']:
                # A restored pending lease can be replaced with a newly reviewed upload.
                db.execute(delete(uploads).where(uploads.c.id == pending['id']))
                pending = None
            if exists or pending:
                raise HTTPException(409, 'artifact_id_exists')
            upload_id = uuid.uuid4().hex
            db.execute(insert(uploads).values(id=upload_id, workspace=workspace, owner_id=user,
                artifact_id=change.artifact_id, sha256=change.sha256, size=change.size,
                visibility=change.visibility, state='staged', history_id=w['history_id']))
            response = {'upload_id':upload_id, 'max_bytes':MAX_BYTES}
            db.execute(insert(receipts).values(workspace=workspace, user_id=user,
                mutation_id=change.mutation_id, digest=digest(request), response=response))
            return response

    @app.put('/v1/workspaces/{workspace}/artifacts/uploads/{upload_id}')
    async def upload(workspace: str, upload_id: str, request: Request, authorization: str = Header(default=''),
                     history_id: str | None = Header(default=None, alias='X-CST-History-ID', max_length=32)):
        # Authorize before reading bytes; do not hold a workspace lock while
        # awaiting the network. Reauthorize after upload before accepting it.
        def inspect_upload():
            with engine.begin() as db:
                user = actor(db, authorization)
                w, role = access(db, workspace, user)
                editor(role)
                require_history(w, history_id)
                row = stage(db, workspace, upload_id, user)
                current_upload(row, w)
                if row['state'] == 'published':
                    raise HTTPException(409, 'artifact_already_published')
                return row['size'], row['sha256'], verified_stage(workspace, row)
        expected_size, expected_sha, ready = await run_in_threadpool(inspect_upload)
        if ready:
            return {'upload_id':upload_id, 'received':expected_size, 'ready':True}
        filename = uuid.uuid4().hex + '.upload'
        temporary = staging / filename
        accepted = False
        remote_attempted = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            hasher = hashlib.sha256()
            total = 0
            with os.fdopen(fd, 'wb') as stream:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > expected_size or total > MAX_BYTES:
                        raise HTTPException(413, 'artifact_too_large')
                    stream.write(chunk)
                    hasher.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if total != expected_size or hasher.hexdigest() != expected_sha:
                raise HTTPException(422, 'artifact_integrity_mismatch')
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            def accept_upload():
                nonlocal remote_attempted
                # Serialize contenders before remote staging. Keep blocking DB/S3
                # work off the event loop so another request cannot deadlock it.
                with locked_transaction(engine) as db:
                    user = actor(db, authorization)
                    w, role = access(db, workspace, user)
                    editor(role)
                    require_history(w, history_id)
                    row = stage(db, workspace, upload_id, user)
                    current_upload(row, w)
                    if row['state'] == 'published':
                        raise HTTPException(409, 'artifact_already_published')
                    if verified_stage(workspace, row):
                        return False
                    if store is not None:
                        remote_attempted = True
                        store.stage(workspace, filename, temporary)
                    db.execute(update(uploads).where(uploads.c.id == upload_id)
                        .values(staged_file=filename, state='uploaded'))
                return True
            accepted = await run_in_threadpool(accept_upload)
            return {'upload_id':upload_id, 'received':total, 'ready':True}
        finally:
            safe_to_remove = not accepted
            if not accepted and (remote_attempted or temporary.exists()):
                def referenced():
                    with engine.connect() as db:
                        return db.execute(select(uploads.c.id).where(uploads.c.staged_file == filename)).first() is not None
                try:
                    # A commit response can fail after the DB committed. Do not
                    # remove bytes until their absence from metadata is confirmed.
                    safe_to_remove = not await run_in_threadpool(referenced)
                    if not safe_to_remove and remote_attempted:
                        # Metadata confirms S3 holds the accepted stage; the local
                        # spool is redundant even if the commit response was lost.
                        temporary.unlink(missing_ok=True)
                    if safe_to_remove and remote_attempted:
                        await run_in_threadpool(store.delete_stage, workspace, filename)
                except Exception:
                    safe_to_remove = False
                    logging.getLogger(__name__).warning('Artifact staging cleanup deferred; storage or database unavailable')
            if safe_to_remove or (accepted and store is not None):
                temporary.unlink(missing_ok=True)

    @app.post('/v1/workspaces/{workspace}/artifacts/uploads/{upload_id}/finalize')
    def finalize(workspace: str, upload_id: str, change: Finalize, authorization: str = Header(default='')):
        request = {'operation':'finalize_artifact', 'upload_id':upload_id, **change.model_dump(exclude_none=True)}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            require_history(w, change.history_id)
            row = stage(db, workspace, upload_id, user)
            current_upload(row, w)
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            if row['state'] != 'uploaded' or not row['staged_file']:
                raise HTTPException(409, 'artifact_not_ready')
            if store is not None:
                store.publish(workspace, row['staged_file'], row['size'], row['sha256'])
            else:
                publish_local(workspace, row)
            record = {'id':row['artifact_id'], 'kind':'artifact', 'owner_id':user,
                'visibility':row['visibility'], 'revision':1, 'deleted':False,
                'body':{'sha256':row['sha256'], 'size':row['size'], 'media_type':'application/octet-stream'}}
            response = commit_record(db, workspace, w, user, change.mutation_id, request, record)
            db.execute(update(uploads).where(uploads.c.id == upload_id).values(state='published'))
            return response

    def publish_local(workspace, row):
        path = staging / row['staged_file']
        try:
            actual_size, actual_sha = hash_file(path)
        except FileNotFoundError:
            raise HTTPException(409, 'artifact_not_ready') from None
        if actual_size != row['size'] or actual_sha != row['sha256']:
            raise HTTPException(422, 'artifact_integrity_mismatch')
        destination = blob_path(workspace, row['sha256'])
        try:
            # Atomic, exclusive link publishes complete bytes. Never overwrite
            # an existing blob. Keep staging until DB commit for crash retries.
            os.link(path, destination)
        except FileExistsError:
            if hash_file(destination) != (row['size'], row['sha256']):
                raise HTTPException(409, 'stored_artifact_integrity_failure') from None
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @app.get('/v1/workspaces/{workspace}/artifacts/{artifact_id}/content')
    def download(workspace: str, artifact_id: str, authorization: str = Header(default='')):
        with engine.begin() as db:
            user = actor(db, authorization)
            access(db, workspace, user)
            record = db.execute(select(records).where(and_(records.c.workspace == workspace,
                records.c.id == artifact_id))).mappings().first()
            if not record or record['kind'] != 'artifact' or record['deleted'] or not visible(record, user):
                raise HTTPException(404, 'artifact_not_found')
            if store is not None:
                return store.download(workspace, record['body']['size'], record['body']['sha256'])
            path = blob_path(workspace, record['body']['sha256'])
            if not path.is_file():
                raise HTTPException(409, 'artifact_unavailable')
            return FileResponse(path, media_type='application/octet-stream', filename='artifact.bin',
                                headers={'Cache-Control':'no-store'})
