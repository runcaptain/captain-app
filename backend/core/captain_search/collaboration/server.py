"""Isolated collaboration contract spike. Not Captain's production backend."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import (JSON, Boolean, Column, Integer, MetaData, String, Table,
                        and_, create_engine, insert, select, update)
from sqlalchemy.pool import StaticPool

metadata = MetaData()
users = Table('sync_users', metadata,
    Column('id', String(100), primary_key=True),
    Column('token_hash', String(64), unique=True, nullable=False))
identities = Table('sync_identities', metadata,
    Column('issuer', String(500), primary_key=True),
    Column('subject', String(500), primary_key=True),
    Column('user_id', String(100), nullable=False))
workspaces = Table('sync_workspaces', metadata,
    Column('id', String(100), primary_key=True),
    Column('cursor', Integer, nullable=False, default=0),
    Column('epoch', Integer, nullable=False, default=1))
members = Table('sync_members', metadata,
    Column('workspace', String(100), primary_key=True),
    Column('user_id', String(100), primary_key=True),
    Column('role', String(10), nullable=False))
records = Table('sync_records', metadata,
    Column('workspace', String(100), primary_key=True),
    Column('id', String(100), primary_key=True),
    Column('kind', String(50), nullable=False),
    Column('owner_id', String(100), nullable=False),
    Column('visibility', String(10), nullable=False),
    Column('revision', Integer, nullable=False),
    Column('body', JSON, nullable=False),
    Column('deleted', Boolean, nullable=False, default=False))
versions = Table('sync_versions', metadata,
    Column('workspace', String(100), primary_key=True),
    Column('record_id', String(100), primary_key=True),
    Column('revision', Integer, primary_key=True),
    Column('record', JSON, nullable=False))
changes = Table('sync_changes', metadata,
    Column('workspace', String(100), primary_key=True),
    Column('cursor', Integer, primary_key=True),
    Column('record', JSON, nullable=False))
receipts = Table('sync_receipts', metadata,
    Column('workspace', String(100), primary_key=True),
    Column('user_id', String(100), primary_key=True),
    Column('mutation_id', String(100), primary_key=True),
    Column('digest', String(64), nullable=False),
    Column('response', JSON, nullable=False))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Mutation(BaseModel):
    mutation_id: str = Field(min_length=1, max_length=100)
    record_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=50)
    expected_revision: int = Field(ge=0)
    visibility: Literal['private', 'workspace'] = 'private'
    body: dict = Field(default_factory=dict)
    deleted: bool = False


class MemberChange(BaseModel):
    role: Literal['reader', 'editor', 'admin', 'revoked']


def visible(record, user):
    return record['visibility'] == 'workspace' or record['owner_id'] == user


def public_record(row):
    return {k: row[k] for k in ('id', 'kind', 'owner_id', 'visibility', 'revision', 'body', 'deleted')}


def prior_receipt(db, workspace, user, mutation_id, request):
    prior = db.execute(select(receipts).where(and_(receipts.c.workspace == workspace,
        receipts.c.user_id == user, receipts.c.mutation_id == mutation_id))).mappings().first()
    if prior:
        if prior['digest'] != digest(request):
            raise HTTPException(409, {'code': 'idempotency_key_reused'})
        return prior['response']
    return None


def commit_record(db, workspace, w, user, mutation_id, request, record, old=None):
    """One writer for domain record, immutable revision, sync event and receipt.

    Caller must hold the workspace row lock and enforce domain authorization.
    """
    where = and_(records.c.workspace == workspace, records.c.id == record['id'])
    if old:
        db.execute(update(records).where(where).values(**record))
    else:
        db.execute(insert(records).values(workspace=workspace, **record))
    cursor = w['cursor'] + 1
    epoch = w['epoch'] + int(bool(old and old['visibility'] != record['visibility']))
    db.execute(update(workspaces).where(workspaces.c.id == workspace).values(cursor=cursor, epoch=epoch))
    db.execute(insert(versions).values(workspace=workspace, record_id=record['id'], revision=record['revision'], record=record))
    db.execute(insert(changes).values(workspace=workspace, cursor=cursor, record=record))
    response = {'record_id': record['id'], 'revision': record['revision'], 'cursor': cursor, 'epoch': epoch}
    db.execute(insert(receipts).values(workspace=workspace, user_id=user, mutation_id=mutation_id,
                                      digest=digest(request), response=response))
    return response


def bootstrap(engine, fixture):
    """Operator-only fixture provisioning. Never exposed over HTTP. No token logs."""
    with engine.begin() as db:
        for user in fixture['users']:
            if not db.execute(select(users.c.id).where(users.c.id == user['id'])).first():
                db.execute(insert(users).values(id=user['id'], token_hash=token_hash(user['token'])))
        for w in fixture['workspaces']:
            if not db.execute(select(workspaces.c.id).where(workspaces.c.id == w['id'])).first():
                db.execute(insert(workspaces).values(id=w['id'], cursor=0, epoch=1))
                for uid, role in w['members'].items():
                    db.execute(insert(members).values(workspace=w['id'], user_id=uid, role=role))


def create_app(database_url=None, fixture=None, artifact_root=None, *, oidc=None, allow_dev_tokens=False, artifact_store=None, auth_mode=None):
    if auth_mode not in (None, 'oidc', 'tokens'):
        raise ValueError('Unsupported authentication mode')
    if sum((bool(oidc), bool(allow_dev_tokens), auth_mode == 'tokens')) > 1:
        raise ValueError("OIDC, production tokens and synthetic token modes are mutually exclusive")
    if auth_mode == 'oidc' and not oidc:
        raise ValueError('OIDC configuration required')
    if not oidc and not allow_dev_tokens and auth_mode != 'tokens':
        raise ValueError("Configure OIDC or explicitly enable synthetic development tokens")
    if fixture and not allow_dev_tokens:
        raise ValueError("Token fixtures require explicit synthetic development mode")
    url = database_url or os.environ.get('DATABASE_URL', 'sqlite:///team-sync.db')
    kwargs = {'pool_pre_ping': True, 'hide_parameters': True}
    if url.startswith('sqlite'):
        kwargs['connect_args'] = {'check_same_thread': False}
        if ':memory:' in url:
            kwargs['poolclass'] = StaticPool
    engine = create_engine(url, **kwargs)
    from .artifacts import register_artifacts
    from .migrations import migrate, require_current
    if allow_dev_tokens:
        migrate(engine)
    else:
        require_current(engine)
    if fixture:
        bootstrap(engine, fixture)
    app = FastAPI(title='Captain collaboration contract spike')
    app.state.engine = engine
    verifier = None
    if oidc:
        from .identity import OIDCVerifier
        verifier = OIDCVerifier(oidc)

    def actor(db, authorization):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'unauthenticated')
        if verifier:
            issuer, subject = verifier.verify(authorization[7:])
            user = db.execute(select(identities.c.user_id).where(and_(identities.c.issuer == issuer, identities.c.subject == subject))).scalar()
        elif auth_mode == 'tokens':
            from .tokens import authenticate
            user = authenticate(db, authorization[7:])
        else:
            user = db.execute(select(users.c.id).where(users.c.token_hash == token_hash(authorization[7:]))).scalar()
        if not user:
            raise HTTPException(401, 'unauthenticated')
        return user

    def access(db, workspace, user):
        # Every reader/writer and ACL change takes the same lock. A committed cursor
        # therefore cannot overtake a transaction whose payload has not committed.
        w = db.execute(select(workspaces).where(workspaces.c.id == workspace).with_for_update()).mappings().first()
        role = db.execute(select(members.c.role).where(and_(members.c.workspace == workspace, members.c.user_id == user))).scalar()
        if not w or role not in ('reader', 'editor', 'admin'):
            raise HTTPException(403, 'workspace_access_denied')
        return w, role

    @app.get('/health')
    def health():
        with engine.connect() as db:
            db.execute(select(1))
        return {'ok': True, 'prototype': True}

    @app.get('/v1/workspaces/{workspace}/identity')
    def workspace_identity(workspace: str, authorization: str = Header(default='')):
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            return {'workspace': workspace, 'user_id': user, 'epoch': w['epoch'], 'role': role}

    @app.get('/v1/workspaces/{workspace}/snapshot')
    def snapshot(workspace: str, authorization: str = Header(default='')):
        with engine.begin() as db:
            user = actor(db, authorization)
            w, _ = access(db, workspace, user)
            rows = db.execute(select(records).where(records.c.workspace == workspace)).mappings()
            data = [public_record(r) for r in rows if visible(r, user) and not r['deleted']]
            return {'workspace': workspace, 'cursor': w['cursor'], 'epoch': w['epoch'], 'records': data}

    @app.get('/v1/workspaces/{workspace}/changes')
    def feed(workspace: str, after: int = Query(ge=0), epoch: int = Query(ge=1),
             limit: int = Query(default=100, ge=1, le=500), authorization: str = Header(default='')):
        with engine.begin() as db:
            user = actor(db, authorization)
            w, _ = access(db, workspace, user)
            if epoch != w['epoch']:
                raise HTTPException(409, {'code': 'reset_required', 'epoch': w['epoch']})
            if after > w['cursor']:
                raise HTTPException(409, {'code': 'reset_required', 'epoch': w['epoch']})
            rows = db.execute(select(changes).where(and_(changes.c.workspace == workspace, changes.c.cursor > after))
                              .order_by(changes.c.cursor).limit(limit)).mappings().all()
            result = []
            for row in rows:
                record = row['record']
                current = db.execute(select(records).where(and_(records.c.workspace == workspace, records.c.id == record['id']))).mappings().first()
                if current and visible(current, user) and visible(record, user):
                    event = {'cursor': row['cursor'], 'record_id': record['id'],
                             'op': 'delete' if record['deleted'] else 'upsert'}
                    if not record['deleted']:
                        event['record'] = record
                    result.append(event)
            cursor = rows[-1]['cursor'] if rows else after
            return {'workspace': workspace, 'epoch': w['epoch'], 'cursor': cursor,
                    'changes': result, 'has_more': cursor < w['cursor']}

    @app.get('/v1/workspaces/{workspace}/records/{record_id}/versions/{revision}')
    def version(workspace: str, record_id: str, revision: int, authorization: str = Header(default='')):
        with engine.begin() as db:
            user = actor(db, authorization)
            access(db, workspace, user)
            current = db.execute(select(records).where(and_(records.c.workspace == workspace, records.c.id == record_id))).mappings().first()
            if not current or not visible(current, user) or current['deleted']:
                raise HTTPException(404, 'record_not_found')
            row = db.execute(select(versions.c.record).where(and_(versions.c.workspace == workspace,
                versions.c.record_id == record_id, versions.c.revision == revision))).scalar()
            if not row or not visible(row, user):
                raise HTTPException(404, 'version_not_found')
            return row

    @app.post('/v1/workspaces/{workspace}/mutations')
    def mutate(workspace: str, change: Mutation, authorization: str = Header(default='')):
        request = change.model_dump()
        if len(json.dumps(request).encode()) > 262144:
            raise HTTPException(413, 'metadata_too_large')
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            if role not in ('editor', 'admin'):
                raise HTTPException(403, 'write_denied')
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            if change.kind in ('job', 'artifact'):
                raise HTTPException(422, 'use_domain_api')
            where = and_(records.c.workspace == workspace, records.c.id == change.record_id)
            old = db.execute(select(records).where(where)).mappings().first()
            if old and not visible(old, user):
                raise HTTPException(404, 'record_not_found')
            if old and old['visibility'] != change.visibility and old['owner_id'] != user:
                raise HTTPException(403, 'only_owner_changes_visibility')
            if change.expected_revision != (old['revision'] if old else 0):
                raise HTTPException(409, {'code': 'revision_conflict', 'current_revision': old['revision'] if old else 0})
            if old and (old['kind'] != change.kind or old['kind'] in ('research_snapshot', 'artifact')):
                raise HTTPException(409, {'code': 'immutable_record'})
            if change.kind == 'research_snapshot':
                # A published snapshot pins immutable record revisions. It contains
                # metadata only; artifact records publish only after byte verification.
                pins = change.body.get('pins')
                if not isinstance(pins, list) or not pins or len(pins) > 100:
                    raise HTTPException(422, 'snapshot_requires_1_to_100_pins')
                for pin in pins:
                    if not isinstance(pin, dict) or not isinstance(pin.get('record_id'), str) or not isinstance(pin.get('revision'), int):
                        raise HTTPException(422, 'invalid_pin')
                    version = db.execute(select(versions.c.record).where(and_(versions.c.workspace == workspace,
                        versions.c.record_id == pin['record_id'], versions.c.revision == pin['revision']))).scalar()
                    current = db.execute(select(records).where(and_(records.c.workspace == workspace, records.c.id == pin['record_id']))).mappings().first()
                    if not version or not current or not visible(current, user) or current['deleted']:
                        raise HTTPException(422, 'pin_not_available')
                    if version['kind'] in ('research_snapshot', 'job'):
                        raise HTTPException(422, 'snapshot_pins_must_be_input_records')
                    if change.visibility == 'workspace' and (current['visibility'] != 'workspace' or version['visibility'] != 'workspace'):
                        raise HTTPException(422, 'shared_snapshot_requires_shared_pins')
            revision = change.expected_revision + 1
            record = {'id': change.record_id, 'kind': change.kind, 'owner_id': old['owner_id'] if old else user,
                      'visibility': change.visibility, 'revision': revision, 'body': change.body, 'deleted': change.deleted}
            return commit_record(db, workspace, w, user, change.mutation_id, request, record, old)

    @app.put('/v1/workspaces/{workspace}/members/{member}')
    def membership(workspace: str, member: str, change: MemberChange, authorization: str = Header(default='')):
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            if role != 'admin':
                raise HTTPException(403, 'admin_required')
            if not db.execute(select(users.c.id).where(users.c.id == member)).first():
                raise HTTPException(404, 'user_not_found')
            where = and_(members.c.workspace == workspace, members.c.user_id == member)
            if db.execute(select(members).where(where)).first():
                db.execute(update(members).where(where).values(role=change.role))
            else:
                db.execute(insert(members).values(workspace=workspace, user_id=member, role=change.role))
            # Reset even on regrant. A cached cursor from an earlier grant cannot
            # be reused to miss records written while the user had no access.
            db.execute(update(workspaces).where(workspaces.c.id == workspace).values(epoch=w['epoch'] + 1))
            return {'epoch': w['epoch'] + 1}

    from .jobs import register_jobs
    register_jobs(app, engine, actor, access)
    register_artifacts(app, engine, actor, access, artifact_root or os.environ.get("ARTIFACT_ROOT", str(Path(__file__).parent / ".artifacts")), store=artifact_store)
    return app


def from_env():
    from .identity import OIDCSettings
    database = os.environ['CST_TEAM_DATABASE_URL']
    artifact_root = os.environ['CST_TEAM_ARTIFACT_ROOT']
    from .artifacts import S3ArtifactStore
    bucket = os.environ.get('CST_TEAM_ARTIFACT_BUCKET')
    store = S3ArtifactStore(bucket, prefix=os.environ.get('CST_TEAM_ARTIFACT_PREFIX', 'team-sync')) if bucket else None
    dev = os.environ.get('CST_TEAM_DEV_TOKENS') == 'synthetic-only'
    mode = os.environ.get('CST_TEAM_AUTH_MODE', 'oidc')
    if mode not in ('oidc', 'tokens') or (dev and 'CST_TEAM_AUTH_MODE' in os.environ):
        raise ValueError('Conflicting or unsupported authentication mode')
    fixture = os.environ.get('CST_TEAM_DEV_FIXTURE')
    oidc = None if dev or mode == 'tokens' else OIDCSettings(
        issuer=os.environ['CST_TEAM_OIDC_ISSUER'],
        audience=os.environ['CST_TEAM_OIDC_AUDIENCE'],
        jwks_url=os.environ['CST_TEAM_OIDC_JWKS_URL'])
    return create_app(database, json.loads(Path(fixture).read_text()) if fixture else None,
                      artifact_root, oidc=oidc, allow_dev_tokens=dev, artifact_store=store,
                      auth_mode=None if dev else mode)
