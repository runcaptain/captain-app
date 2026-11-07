"""Durable job-state contract. This module never starts or executes a workload."""
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from typing import Literal

from .server import (commit_record, members, prior_receipt, public_record,
                    records, versions, visible, require_history)

TERMINAL = {'succeeded', 'failed', 'cancelled'}


class HistoryChange(BaseModel):
    history_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class Submit(HistoryChange):
    mutation_id: str = Field(min_length=1, max_length=100)
    job_id: str = Field(min_length=1, max_length=100)
    snapshot_id: str = Field(min_length=1, max_length=100)
    snapshot_revision: int = Field(ge=1)
    visibility: Literal['private', 'workspace'] = 'private'


class Attempt(HistoryChange):
    mutation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    runner_id: str = Field(min_length=1, max_length=100)


class Event(HistoryChange):
    mutation_id: str = Field(min_length=1, max_length=100)
    attempt: int = Field(ge=1)
    sequence: int = Field(ge=1)
    state: Literal['running', 'succeeded', 'failed', 'cancelled']
    progress: float = Field(ge=0, le=1, allow_inf_nan=False)


class Cancel(HistoryChange):
    mutation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


def register_jobs(app, engine, actor, access):
    def get_job(db, workspace, job_id, user):
        old = db.execute(select(records).where(and_(records.c.workspace == workspace,
            records.c.id == job_id))).mappings().first()
        if not old or old['kind'] != 'job' or old['deleted'] or not visible(old, user):
            raise HTTPException(404, 'job_not_found')
        return old

    def available_snapshot(db, workspace, snapshot_id, snapshot_revision, user, shared=False):
        current = db.execute(select(records).where(and_(records.c.workspace == workspace,
            records.c.id == snapshot_id))).mappings().first()
        snapshot = db.execute(select(versions.c.record).where(and_(versions.c.workspace == workspace,
            versions.c.record_id == snapshot_id, versions.c.revision == snapshot_revision))).scalar()
        if not current or not snapshot or current['kind'] != 'research_snapshot' or current['deleted'] or not visible(current, user) or not visible(snapshot, user):
            raise HTTPException(422, 'snapshot_not_available')
        if shared and (current['visibility'] != 'workspace' or snapshot['visibility'] != 'workspace'):
            raise HTTPException(422, 'shared_job_requires_shared_snapshot')
        for pin in snapshot['body']['pins']:
            current_pin = db.execute(select(records).where(and_(records.c.workspace == workspace,
                records.c.id == pin['record_id']))).mappings().first()
            pinned = db.execute(select(versions.c.record).where(and_(versions.c.workspace == workspace,
                versions.c.record_id == pin['record_id'], versions.c.revision == pin['revision']))).scalar()
            if not current_pin or not pinned or current_pin['deleted'] or not visible(current_pin, user) or not visible(pinned, user):
                raise HTTPException(422, 'snapshot_pin_not_available')
            if shared and (current_pin['visibility'] != 'workspace' or pinned['visibility'] != 'workspace'):
                raise HTTPException(422, 'shared_job_requires_shared_pins')

    def editor(role):
        if role not in ('editor', 'admin'):
            raise HTTPException(403, 'write_denied')

    def control(old, user, role):
        if old['owner_id'] != user and role != 'admin':
            raise HTTPException(403, 'job_owner_required')

    def revision(old, expected):
        if old['revision'] != expected:
            raise HTTPException(409, {'code':'revision_conflict', 'current_revision':old['revision']})

    def save(db, workspace, w, user, request, change, old, body):
        record = dict(public_record(old), revision=old['revision'] + 1, body=body)
        return commit_record(db, workspace, w, user, change.mutation_id, request, record, old)

    @app.post('/v1/workspaces/{workspace}/jobs')
    def submit(workspace: str, change: Submit, authorization: str = Header(default='')):
        request = {'operation':'submit_job', **change.model_dump(exclude_none=True)}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            require_history(w, change.history_id)
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            existing = db.execute(select(records.c.id).where(and_(records.c.workspace == workspace,
                records.c.id == change.job_id))).first()
            if existing:
                raise HTTPException(409, 'job_id_exists')
            available_snapshot(db, workspace, change.snapshot_id, change.snapshot_revision,
                               user, change.visibility == 'workspace')
            record = {'id':change.job_id, 'kind':'job', 'owner_id':user, 'visibility':change.visibility,
                'revision':1, 'deleted':False, 'body':{'snapshot_id':change.snapshot_id,
                'snapshot_revision':change.snapshot_revision, 'state':'queued', 'attempt':0,
                'runner_id':None, 'event_sequence':0, 'progress':0.0, 'cancel_requested':False}}
            return commit_record(db, workspace, w, user, change.mutation_id, request, record)

    @app.post('/v1/workspaces/{workspace}/jobs/{job_id}/attempts')
    def attempt(workspace: str, job_id: str, change: Attempt, authorization: str = Header(default='')):
        request = {'operation':'start_attempt', 'job_id':job_id, **change.model_dump(exclude_none=True)}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            require_history(w, change.history_id)
            old = get_job(db, workspace, job_id, user)
            control(old, user, role)
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            revision(old, change.expected_revision)
            body = old['body']
            if body['state'] in ('succeeded', 'cancelled') or body['cancel_requested']:
                raise HTTPException(409, 'job_cannot_start_attempt')
            runner_role = db.execute(select(members.c.role).where(and_(members.c.workspace == workspace,
                members.c.user_id == change.runner_id))).scalar()
            if runner_role not in ('editor', 'admin') or (old['visibility'] == 'private' and change.runner_id != old['owner_id']):
                raise HTTPException(422, 'runner_not_authorized')
            available_snapshot(db, workspace, body['snapshot_id'], body['snapshot_revision'], change.runner_id)
            body = dict(body, state='running', attempt=body['attempt'] + 1, runner_id=change.runner_id,
                        event_sequence=0, progress=0.0, attempt_history_id=w['history_id'])
            return save(db, workspace, w, user, request, change, old, body)

    @app.post('/v1/workspaces/{workspace}/jobs/{job_id}/events')
    def event(workspace: str, job_id: str, change: Event, authorization: str = Header(default='')):
        request = {'operation':'runner_event', 'job_id':job_id, **change.model_dump(exclude_none=True)}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            require_history(w, change.history_id)
            old = get_job(db, workspace, job_id, user)
            body = old['body']
            if body['runner_id'] != user:
                raise HTTPException(403, 'assigned_runner_required')
            if body.get('attempt_history_id') != w['history_id'] and (w['history_required'] or body.get('attempt_history_id') is not None):
                raise HTTPException(409, 'stale_attempt_history')
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            if change.attempt != body['attempt']:
                raise HTTPException(409, 'stale_attempt')
            if body['state'] in TERMINAL:
                raise HTTPException(409, 'terminal_state')
            if change.sequence != body['event_sequence'] + 1:
                raise HTTPException(409, 'event_sequence_conflict')
            if change.progress < body['progress']:
                raise HTTPException(409, 'progress_regression')
            if change.state == 'succeeded' and change.progress != 1:
                raise HTTPException(422, 'success_requires_complete_progress')
            if change.state == 'cancelled' and not body['cancel_requested']:
                raise HTTPException(409, 'cancellation_not_requested')
            body = dict(body, state=change.state, progress=change.progress, event_sequence=change.sequence)
            return save(db, workspace, w, user, request, change, old, body)

    @app.post('/v1/workspaces/{workspace}/jobs/{job_id}/cancel')
    def cancel(workspace: str, job_id: str, change: Cancel, authorization: str = Header(default='')):
        request = {'operation':'request_cancel', 'job_id':job_id, **change.model_dump(exclude_none=True)}
        with engine.begin() as db:
            user = actor(db, authorization)
            w, role = access(db, workspace, user)
            editor(role)
            require_history(w, change.history_id)
            old = get_job(db, workspace, job_id, user)
            control(old, user, role)
            prior = prior_receipt(db, workspace, user, change.mutation_id, request)
            if prior:
                return prior
            revision(old, change.expected_revision)
            if old['body']['state'] in TERMINAL:
                raise HTTPException(409, 'terminal_state')
            # A queued job has never been assigned, so the server can confirm
            # cancellation atomically. Running work needs runner confirmation.
            body = dict(old['body'], cancel_requested=True)
            if body['state'] == 'queued':
                body['state'] = 'cancelled'
            return save(db, workspace, w, user, request, change, old, body)
