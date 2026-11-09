"""An open desktop editor cannot inherit a newer cache revision on save."""
from contextlib import contextmanager

import httpx
import sqlite3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_search.collaboration.client import SyncClient
from captain_search.collaboration import routes
from test_sync_cli import remote  # noqa: F401


def test_pull_during_edit_and_resolution_preserves_reviewed_revision(remote, tmp_path, monkeypatch):
    with httpx.Client(base_url=remote, headers={'Authorization':'Bearer alice-cli-test'}) as alice_http, \
         httpx.Client(base_url=remote, headers={'Authorization':'Bearer bob-cli-test'}) as bob_http:
        alice = SyncClient(tmp_path/'alice.sqlite', alice_http, 'team', 'alice')
        bob = SyncClient(tmp_path/'bob.sqlite', bob_http, 'team', 'bob')
        # The HTTP route runs in TestClient's worker thread; requests remain serial.
        bob.db.close()
        bob.db = sqlite3.connect(tmp_path/'bob.sqlite', check_same_thread=False)
        @contextmanager
        def connection():
            yield bob
        monkeypatch.setattr(routes, 'connection', connection)
        app = FastAPI()
        app.include_router(routes.router)
        try:
            alice.pull()
            alice.queue_edit('review', 'config', {'value':1}, 'workspace')
            alice.flush()
            bob.pull()
            selected_revision = bob.get('review')['revision']
            alice.queue_edit('review', 'config', {'value':2})
            alice.flush()
            bob.pull()
            with TestClient(app) as desktop:
                saved = desktop.put('/api/team/records/review/draft', json={
                    'body':{'value':'stale edit'}, 'expected_revision':selected_revision})
                assert saved.status_code == 200
                assert bob.drafts()[0]['mutation']['expected_revision'] == 1
                bob.flush()
                draft = bob.drafts()[0]
                assert draft['state'] == 'conflict'
                assert bob.get('review')['body'] == {'value':2}
                reviewed_revision = bob.get('review')['revision']
                alice.queue_edit('review', 'config', {'value':3})
                alice.flush()
                bob.pull()
                resolved = desktop.post('/api/team/drafts/'+draft['mutation']['mutation_id']+'/resolve', json={
                    'body':{'value':'stale resolution'}, 'expected_revision':reviewed_revision})
                assert resolved.status_code == 200
                assert bob.drafts()[0]['mutation']['expected_revision'] == 2
                bob.flush()
                assert bob.drafts()[0]['state'] == 'conflict'
                assert bob.get('review')['body'] == {'value':3}
                # Another local process can refresh history while this editor remains open.
                old_history = 'old-history'
                with bob.db:
                    bob._set('history_id', 'replacement-history')
                before = bob.drafts()
                stale = {'body':{'value':'old history'}, 'expected_revision':3, 'expected_history':old_history}
                assert desktop.put('/api/team/records/review/draft', json=stale).status_code == 409
                assert desktop.post('/api/team/drafts/'+before[0]['mutation']['mutation_id']+'/resolve', json=stale).status_code == 409
                assert bob.drafts() == before
                # Explicit null is the history reviewed by a legacy-cache editor,
                # not permission to adopt a newly restored history automatically.
                stale['expected_history'] = None
                assert desktop.put('/api/team/records/review/draft', json=stale).status_code == 409
                assert desktop.post('/api/team/drafts/'+before[0]['mutation']['mutation_id']+'/resolve', json=stale).status_code == 409
                assert bob.drafts() == before
                # Omitted history remains compatible with existing CLI/API callers.
                del stale['expected_history']
                assert desktop.put('/api/team/records/legacy-caller/draft', json=stale).status_code == 200
                assert desktop.get('/api/team/records').json()['history_id'] == 'replacement-history'
        finally:
            alice.close()
            bob.close()
