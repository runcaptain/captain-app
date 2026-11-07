"""Bounded artifact publication with local or private S3 storage."""
from __future__ import annotations

import hashlib
import os
import uuid
import re
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Table, UniqueConstraint, and_, insert, select, update

from .server import (commit_record, digest, metadata, prior_receipt, receipts,
                    records, visible)

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
    UniqueConstraint('workspace', 'artifact_id'))


class Begin(BaseModel):
    mutation_id: str = Field(min_length=1, max_length=100)
    artifact_id: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=0, le=MAX_BYTES)
    visibility: Literal['private', 'workspace'] = 'private'


class Finalize(BaseModel):
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

    @app.post('/v1/workspaces/{workspace}/artifacts/uploads')
    def begin(workspace: str, change: Begin, authorization: str = Header(default='')):
        request = {'operation':'begin_artifact', **change.model_dump()}
        with engine.begin() as db:
            user = actor(db, authorization)
            _, role = access(db, workspace, user)
            editor(role)
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            exists = db.execute(select(records.c.id).where(and_(records.c.workspace == workspace,
                records.c.id == change.artifact_id))).first()
            pending = db.execute(select(uploads.c.id).where(and_(uploads.c.workspace == workspace,
                uploads.c.artifact_id == change.artifact_id))).first()
            if exists or pending:
                raise HTTPException(409, 'artifact_id_exists')
            upload_id = uuid.uuid4().hex
            db.execute(insert(uploads).values(id=upload_id, workspace=workspace, owner_id=user,
                artifact_id=change.artifact_id, sha256=change.sha256, size=change.size,
                visibility=change.visibility, state='staged'))
            response = {'upload_id':upload_id, 'max_bytes':MAX_BYTES}
            db.execute(insert(receipts).values(workspace=workspace, user_id=user,
                mutation_id=change.mutation_id, digest=digest(request), response=response))
            return response

    @app.put('/v1/workspaces/{workspace}/artifacts/uploads/{upload_id}')
    async def upload(workspace: str, upload_id: str, request: Request, authorization: str = Header(default='')):
        # Authorize before reading bytes; do not hold a workspace lock while
        # awaiting the network. Reauthorize after upload before accepting it.
        with engine.begin() as db:
            user = actor(db, authorization)
            _, role = access(db, workspace, user)
            editor(role)
            row = stage(db, workspace, upload_id, user)
            if row['state'] == 'published':
                raise HTTPException(409, 'artifact_already_published')
            expected_size, expected_sha = row['size'], row['sha256']
        filename = uuid.uuid4().hex + '.upload'
        temporary = staging / filename
        accepted = False
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
            if store is not None:
                await run_in_threadpool(store.stage, workspace, filename, temporary)
            with engine.begin() as db:
                user = actor(db, authorization)
                _, role = access(db, workspace, user)
                editor(role)
                row = stage(db, workspace, upload_id, user)
                if row['state'] == 'published':
                    raise HTTPException(409, 'artifact_already_published')
                db.execute(update(uploads).where(uploads.c.id == upload_id)
                    .values(staged_file=filename, state='uploaded'))
            accepted = True
            return {'upload_id':upload_id, 'received':total, 'ready':True}
        finally:
            if not accepted or store is not None:
                temporary.unlink(missing_ok=True)

    @app.post('/v1/workspaces/{workspace}/artifacts/uploads/{upload_id}/finalize')
    def finalize(workspace: str, upload_id: str, change: Finalize, authorization: str = Header(default='')):
        request = {'operation':'finalize_artifact', 'upload_id':upload_id, **change.model_dump()}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            row = stage(db, workspace, upload_id, user)
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
