"""Restored histories fence domain receipts, upload leases and running attempts."""
import uuid

import pytest

from sqlalchemy import update

from captain_search.collaboration.server import workspaces
from test_artifacts import DATA, begin
from test_jobs import setup_job, attempt, event, job
from test_server import team  # noqa: F401


def restore(app, workspace):
    history = uuid.uuid4().hex
    with app.state.engine.begin() as db:
        db.execute(update(workspaces).where(workspaces.c.id == workspace)
                   .values(history_id=history, history_required=True))
    return history


def test_restored_jobs_reject_old_receipts_and_runner_attempts(team):
    c, app, w, alice, bob, _, headers = team
    root, submitted, _ = setup_job(team)
    old_history = c.get(root+'/snapshot', headers=headers[alice]).json()['history_id']
    started = attempt(bob)
    assert c.post(root+'/jobs/job/attempts', headers=headers[alice], json=started).status_code == 200
    reported = event()
    assert c.post(root+'/jobs/job/events', headers=headers[bob], json=reported).status_code == 200
    history = restore(app, w)
    actions = [('/jobs', submitted, alice), ('/jobs/job/attempts', started, alice),
               ('/jobs/job/events', reported, bob),
               ('/jobs/job/cancel', {'mutation_id':uuid.uuid4().hex, 'expected_revision':3}, alice)]
    for path, body, actor in actions:
        for stale in (body, dict(body, history_id=old_history)):
            result = c.post(root+path, headers=headers[actor], json=stale)
            assert result.status_code == 409, result.text
            assert result.json()['detail']['code'] == 'history_reset_required'
    # Knowing the current UUID does not grant an old worker a new attempt lease.
    late = dict(event('succeeded', sequence=2, progress=1), history_id=history)
    denied = c.post(root+'/jobs/job/events', headers=headers[bob], json=late)
    assert denied.status_code == 409 and denied.json()['detail'] == 'stale_attempt_history'
    restart = dict(attempt(bob, revision=3), history_id=history)
    assert c.post(root+'/jobs/job/attempts', headers=headers[alice], json=restart).status_code == 200
    cancel = {'mutation_id':uuid.uuid4().hex, 'expected_revision':4, 'history_id':history}
    assert c.post(root+'/jobs/job/cancel', headers=headers[alice], json=cancel).status_code == 200
    confirmation = dict(event('cancelled', attempt=2), history_id=history)
    assert c.post(root+'/jobs/job/events', headers=headers[bob], json=confirmation).status_code == 200
    assert job(c, root, headers[alice])['body']['state'] == 'cancelled'


def test_restored_artifacts_reject_old_upload_leases_and_receipts(team):
    c, app, w, alice, _, _, headers = team
    root = f'/v1/workspaces/{w}'
    upload, request = begin(c, root, headers[alice], artifact='pending')
    endpoint = root+'/artifacts/uploads/'+upload
    assert c.put(endpoint, headers=headers[alice], content=DATA).status_code == 200
    published, _ = begin(c, root, headers[alice], artifact='published')
    published_endpoint = root+'/artifacts/uploads/'+published
    assert c.put(published_endpoint, headers=headers[alice], content=DATA).status_code == 200
    finalize_receipt = {'mutation_id':uuid.uuid4().hex}
    assert c.post(published_endpoint+'/finalize', headers=headers[alice], json=finalize_receipt).status_code == 200
    old_history = c.get(root+'/snapshot', headers=headers[alice]).json()['history_id']
    history = restore(app, w)
    assert c.get(root+'/artifacts/published/content', headers=headers[alice]).content == DATA
    for stale in (None, old_history):
        body = dict(request)
        finalize = dict(finalize_receipt)
        upload_headers = dict(headers[alice])
        if stale:
            body['history_id'] = stale
            finalize['history_id'] = stale
            upload_headers['X-CST-History-ID'] = stale
        assert c.post(root+'/artifacts/uploads', headers=headers[alice], json=body).status_code == 409
        assert c.post(published_endpoint+'/finalize', headers=headers[alice], json=finalize).status_code == 409
        assert c.put(endpoint, headers=upload_headers, content=DATA).status_code == 409
    current_headers = dict(headers[alice], **{'X-CST-History-ID':history})
    assert c.put(endpoint, headers=current_headers, content=DATA).json()['detail'] == 'stale_upload_history'
    assert c.post(endpoint+'/finalize', headers=headers[alice], json={
        'mutation_id':uuid.uuid4().hex, 'history_id':history}).json()['detail'] == 'stale_upload_history'
    # The same artifact ID can be reviewed and uploaded again with a fresh lease.
    replacement = c.post(root+'/artifacts/uploads', headers=headers[alice], json=dict(
        request, mutation_id=uuid.uuid4().hex, history_id=history))
    assert replacement.status_code == 200, replacement.text
    new_endpoint = root+'/artifacts/uploads/'+replacement.json()['upload_id']
    assert c.put(new_endpoint, headers=current_headers, content=DATA).status_code == 200
    final = c.post(new_endpoint+'/finalize', headers=headers[alice], json={
        'mutation_id':uuid.uuid4().hex, 'history_id':history})
    assert final.status_code == 200, final.text
    assert c.get(root+'/artifacts/pending/content', headers=headers[alice]).content == DATA


def test_history_change_during_artifact_stream_is_rechecked(team):
    c, app, w, alice, _, _, headers = team
    root = f'/v1/workspaces/{w}'
    upload, _ = begin(c, root, headers[alice])
    endpoint = root+'/artifacts/uploads/'+upload
    def stream():
        yield DATA[:3]
        restore(app, w)
        yield DATA[3:]
    response = c.put(endpoint, headers=headers[alice], content=stream())
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'history_reset_required'
    assert c.get(root+'/snapshot', headers=headers[alice]).json()['records'] == []


@pytest.mark.parametrize('cancel_before_restore', [False, True])
@pytest.mark.parametrize('administrator', [False, True])
def test_restored_running_job_can_finish_cancellation_without_stale_runner(team, cancel_before_restore, administrator):
    c, app, workspace, alice, bob, _, headers = team
    root, _, _ = setup_job(team)
    assert c.post(root+'/jobs/job/attempts', headers=headers[alice], json=attempt(bob)).status_code == 200
    if cancel_before_restore:
        before = {'mutation_id': uuid.uuid4().hex, 'expected_revision': 2}
        assert c.post(root+'/jobs/job/cancel', headers=headers[alice], json=before).status_code == 200
        assert job(c, root, headers[alice])['body']['state'] == 'running'
    history = restore(app, workspace)
    revision = job(c, root, headers[alice])['revision']
    request = {'mutation_id': uuid.uuid4().hex, 'expected_revision': revision, 'history_id': history}
    # Merely being the old assigned worker is not authority to close the job.
    denied = c.post(root+'/jobs/job/cancel', headers=headers[bob], json=request)
    assert denied.status_code == 403 and denied.json()['detail'] == 'job_owner_required'
    # Same-history events are still fenced against the old attempt's history.
    late = dict(event('cancelled'), history_id=history)
    assert c.post(root+'/jobs/job/events', headers=headers[bob], json=late).json()['detail'] == 'stale_attempt_history'
    actor = alice
    if administrator:
        promoted = c.put(root+'/members/'+bob, headers=headers[alice], json={'role': 'admin', 'history_id': history})
        assert promoted.status_code == 200, promoted.text
        actor = bob
    stale = dict(request, mutation_id=uuid.uuid4().hex, expected_revision=revision-1)
    assert c.post(root+'/jobs/job/cancel', headers=headers[actor], json=stale).status_code == 409
    result = c.post(root+'/jobs/job/cancel', headers=headers[actor], json=request)
    assert result.status_code == 200, result.text
    assert c.post(root+'/jobs/job/cancel', headers=headers[actor], json=request).json() == result.json()
    final = job(c, root, headers[alice])
    assert final['revision'] == revision + 1
    assert final['body']['state'] == 'cancelled' and final['body']['cancel_requested'] is True
    assert final['body']['cancellation_reason'] == 'restored_attempt_fenced'
    assert final['body']['attempt'] == 1
    assert final['body']['attempt_history_id'] != history
    assert c.post(root+'/jobs/job/events', headers=headers[bob], json=late).status_code == 409
    restart = dict(attempt(bob, revision=final['revision']), history_id=history)
    assert c.post(root+'/jobs/job/attempts', headers=headers[actor], json=restart).status_code == 409
