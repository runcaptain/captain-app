import httpx
import pytest
try:
    import httpx2
    HTTP_ERRORS = (httpx.HTTPStatusError, httpx2.HTTPStatusError)
except ImportError:
    HTTP_ERRORS = (httpx.HTTPStatusError,)
from fastapi.testclient import TestClient
from client import SyncClient
from server import create_app


@pytest.fixture
def clients(tmp_path):
    fixture = {'users': [{'id':'alice','token':'alice-test'},{'id':'bob','token':'bob-test'}],
               'workspaces':[{'id':'team','members':{'alice':'admin','bob':'editor'}}]}
    app = create_app('sqlite:///:memory:', fixture, allow_dev_tokens=True)
    a = TestClient(app, headers={'Authorization':'Bearer alice-test'})
    b = TestClient(app, headers={'Authorization':'Bearer bob-test'})
    alice = SyncClient(tmp_path/'alice.db', a, 'team', 'alice')
    bob = SyncClient(tmp_path/'bob.db', b, 'team', 'bob')
    alice.bootstrap(); bob.bootstrap()
    yield alice,bob,a,b
    alice.close();bob.close()


def test_offline_edits_survive_restart_and_conflict_is_explicit(clients,tmp_path):
    alice,bob,a,b=clients
    alice.queue_edit('config','config',{'weight':1},'workspace');alice.flush();bob.pull()
    bob.queue_edit('config','config',{'weight':2},'workspace')
    # A separate process can reopen the durable draft before reconnecting.
    reopened=SyncClient(tmp_path/'bob.db',b,'team','bob')
    assert reopened.drafts()[0]['mutation']['body']=={'weight':2}
    alice.queue_edit('config','config',{'weight':3},'workspace');alice.flush()
    reopened.flush()
    assert reopened.drafts()[0]['state']=='conflict'
    assert reopened.get('config')['body']=={'weight':3}
    reopened.close()


def test_private_transition_clears_cached_record_and_pending_draft(clients):
    alice,bob,a,b=clients
    alice.queue_edit('config','config',{'secret':'shared'},'workspace');alice.flush();bob.pull()
    bob.queue_edit('config','config',{'secret':'queued'},'workspace')
    alice.queue_edit('config','config',{'secret':'now private'},'private');alice.flush()
    bob.pull()
    assert bob.get('config') is None
    assert bob.drafts()==[]


def test_revoked_workspace_purges_cache_and_queued_edits(clients):
    alice,bob,a,b=clients
    alice.queue_edit('shared','investigation',{'title':'test'},'workspace');alice.flush();bob.pull()
    bob.queue_edit('new','config',{'title':'offline'})
    response=a.put('/v1/workspaces/team/members/bob',json={'role':'revoked'})
    assert response.status_code==200
    with pytest.raises(HTTP_ERRORS) as error:
        bob.pull()
    assert error.value.response.status_code == 403
    assert bob.get('shared') is None and bob.drafts()==[]


def test_cache_cannot_switch_identity(clients,tmp_path):
    alice,bob,a,b=clients
    with pytest.raises(ValueError): SyncClient(tmp_path/'alice.db',b,'team','bob')


def test_lost_ack_reuses_durable_mutation_id(clients):
    alice,bob,a,b=clients
    mutation_id=alice.queue_edit('retry','config',{'value':7},'workspace')
    payload=alice.drafts()[0]['mutation']
    # Server committed, client lost response before removing outbox row.
    first=a.post('/v1/workspaces/team/mutations',json=payload)
    assert first.status_code==200
    alice.flush()
    assert alice.drafts()==[]
    assert alice.get('retry')['revision']==1
    assert a.post('/v1/workspaces/team/mutations',json=payload).json()==first.json()


def test_flush_refreshes_permissions_before_upload(clients):
    alice,bob,a,b=clients
    alice.queue_edit('shared','config',{'value':1},'workspace');alice.flush();bob.pull()
    bob.queue_edit('shared','config',{'value':2})
    alice.queue_edit('shared','config',{'value':3},'private');alice.flush()
    bob.flush()
    assert bob.get('shared') is None and bob.drafts()==[]


def test_edit_preserves_existing_visibility(clients):
    alice,bob,a,b=clients
    alice.queue_edit('shared','config',{'value':1},'workspace');alice.flush()
    alice.queue_edit('shared','config',{'value':2});alice.flush();bob.pull()
    assert bob.get('shared')['body']=={'value':2}
