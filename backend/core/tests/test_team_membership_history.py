"""Restored workspace ACL writes require the newly reviewed history."""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import update

from captain_search.collaboration.server import create_app, workspaces


def test_membership_history_fence_preserves_acl_until_current_admin_request(tmp_path):
    app = create_app('sqlite:///' + str(tmp_path / 'server.db'), {
        'users': [{'id': 'alice', 'token': 'synthetic-alice'}, {'id': 'bob', 'token': 'synthetic-bob'}],
        'workspaces': [{'id': 'team', 'members': {'alice': 'admin', 'bob': 'reader'}}]},
        allow_dev_tokens=True, artifact_root=tmp_path / 'artifacts')
    alice = {'Authorization': 'Bearer synthetic-alice'}
    bob = {'Authorization': 'Bearer synthetic-bob'}
    base = '/v1/workspaces/team'
    try:
        with TestClient(app) as client:
            initial = client.get(base + '/identity', headers=alice).json()
            # Legacy requests remain supported until a restore rotates history.
            assert client.put(base + '/members/bob', headers=alice, json={'role': 'editor'}).status_code == 200
            before = client.get(base + '/identity', headers=alice).json()
            history = uuid.uuid4().hex
            with app.state.engine.begin() as db:
                db.execute(update(workspaces).where(workspaces.c.id == 'team')
                           .values(history_id=history, history_required=True))
            for change in ({'role': 'admin'}, {'role': 'admin', 'history_id': None},
                           {'role': 'admin', 'history_id': initial['history_id']}):
                response = client.put(base + '/members/bob', headers=alice, json=change)
                assert response.status_code == 409
                assert response.json()['detail']['code'] == 'history_reset_required'
                assert client.get(base + '/identity', headers=bob).json()['role'] == 'editor'
                assert client.get(base + '/identity', headers=alice).json()['epoch'] == before['epoch']
            # A current history does not grant administrator privileges.
            response = client.put(base + '/members/bob', headers=bob,
                                  json={'role': 'admin', 'history_id': history})
            assert response.status_code == 403
            assert response.json()['detail'] == 'admin_required'
            response = client.put(base + '/members/bob', headers=alice,
                                  json={'role': 'revoked', 'history_id': history})
            assert response.status_code == 200
            assert response.json()['epoch'] == before['epoch'] + 1
            assert client.get(base + '/identity', headers=bob).status_code == 403
    finally:
        app.state.engine.dispose()
