"""Run DATABASE_URL=postgresql+psycopg://... pytest -q test_server.py.
Uses unique workspace/user IDs per test. Never drops existing database tables.
"""
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from server import create_app, workspaces


@pytest.fixture
def team(tmp_path):
    suffix = uuid.uuid4().hex[:12]
    alice, bob, outsider = [f'{name}-{suffix}' for name in ('alice', 'bob', 'outsider')]
    tokens = {name: secrets.token_urlsafe(24) for name in (alice, bob, outsider)}
    workspace = f'team-{suffix}'
    app = create_app(os.environ.get('DATABASE_URL', 'sqlite:///:memory:'), {
        'users': [{'id': uid, 'token': token} for uid, token in tokens.items()],
        'workspaces': [{'id': workspace, 'members': {alice: 'admin', bob: 'editor'}}],
    }, allow_dev_tokens=True, artifact_root=tmp_path/'artifacts')
    client = TestClient(app)
    yield client, app, workspace, alice, bob, outsider, {u: {'Authorization': f'Bearer {t}'} for u, t in tokens.items()}
    client.close()
    app.state.engine.dispose()


def mutation(record='config', revision=0, visibility='workspace', **extra):
    return dict(mutation_id=uuid.uuid4().hex, record_id=record, kind='config',
                expected_revision=revision, visibility=visibility, body={'metric': 'ndcg'}, **extra)


def test_two_users_snapshot_delta_idempotency_and_conflict(team):
    c, app, w, a, b, _, h = team
    root = f'/v1/workspaces/{w}'
    baseline = c.get(root+'/snapshot', headers=h[b]).json()
    change = mutation()
    first = c.post(root+'/mutations', headers=h[a], json=change)
    assert first.status_code == 200
    assert c.post(root+'/mutations', headers=h[a], json=change).json() == first.json()
    changed_payload = dict(change, body={'metric': 'mrr'})
    assert c.post(root+'/mutations', headers=h[a], json=changed_payload).status_code == 409
    conflict = c.post(root+'/mutations', headers=h[b], json=mutation())
    assert conflict.status_code == 409
    feed = c.get(root+'/changes', headers=h[b], params={'after': baseline['cursor'], 'epoch': baseline['epoch']}).json()
    assert feed['cursor'] == 1 and len(feed['changes']) == 1
    assert feed['changes'][0]['record']['body']['metric'] == 'ndcg'
    assert not feed['has_more']


def test_private_data_and_old_history_are_not_exposed(team):
    c, _, w, a, b, outsider, h = team
    root = f'/v1/workspaces/{w}'
    assert c.get(root+'/snapshot', headers=h[outsider]).status_code == 403
    assert c.get(root+'/snapshot').status_code == 401
    c.post(root+'/mutations', headers=h[a], json=mutation('secret', visibility='private'))
    assert c.get(root+'/snapshot', headers=h[b]).json()['records'] == []
    empty_feed = c.get(root+'/changes', headers=h[b], params={'after': 0, 'epoch': 1}).json()
    assert empty_feed['changes'] == [] and empty_feed['cursor'] == 1
    assert c.post(root+'/mutations', headers=h[b], json=mutation('secret', 1)).status_code == 404
    c.post(root+'/mutations', headers=h[a], json=mutation('shared'))
    before = c.get(root+'/snapshot', headers=h[b]).json()
    c.post(root+'/mutations', headers=h[a], json=mutation('shared', 1, 'private'))
    assert c.get(root+'/changes', headers=h[b], params={'after': before['cursor'], 'epoch': before['epoch']}).status_code == 409
    after = c.get(root+'/snapshot', headers=h[b]).json()
    assert after['records'] == []
    # A curious client cannot use the new epoch to retrieve old public content.
    history = c.get(root+'/changes', headers=h[b], params={'after': 0, 'epoch': after['epoch']}).json()
    assert history['changes'] == []
    assert c.get(root+'/records/shared/versions/1', headers=h[b]).status_code == 404


def test_revocation_and_regrant_reset(team):
    c, _, w, a, b, _, h = team
    root = f'/v1/workspaces/{w}'
    old = c.get(root+'/snapshot', headers=h[b]).json()
    assert c.put(root+f'/members/{b}', headers=h[a], json={'role':'revoked'}).status_code == 200
    assert c.get(root+'/snapshot', headers=h[b]).status_code == 403
    assert c.get(root+'/changes', headers=h[b], params={'after':0,'epoch':old['epoch']}).status_code == 403
    assert c.post(root+'/mutations', headers=h[b], json=mutation()).status_code == 403
    c.post(root+'/mutations', headers=h[a], json=mutation())
    c.put(root+f'/members/{b}', headers=h[a], json={'role':'reader'})
    assert c.get(root+'/changes', headers=h[b], params={'after':0,'epoch':old['epoch']}).status_code == 409
    assert len(c.get(root+'/snapshot', headers=h[b]).json()['records']) == 1
    assert c.post(root+'/mutations', headers=h[b], json=mutation('other')).status_code == 403


def test_snapshot_pins_are_immutable_and_access_checked(team):
    c, _, w, a, b, _, h = team
    root=f'/v1/workspaces/{w}'
    c.post(root+'/mutations', headers=h[a], json=mutation())
    snap=mutation('snapshot');snap['kind']='research_snapshot';snap['body']={'pins':[{'record_id':'config','revision':1}]}
    assert c.post(root+'/mutations', headers=h[a], json=snap).status_code == 200
    snap.update(mutation_id=uuid.uuid4().hex, expected_revision=1)
    assert c.post(root+'/mutations', headers=h[a], json=snap).status_code == 409
    edit=mutation('config',1);edit['body']={'metric':'mrr'}
    c.post(root+'/mutations', headers=h[a], json=edit)
    assert c.get(root+'/records/config/versions/1',headers=h[b]).json()['body']['metric']=='ndcg'
    assert c.get(root+'/records/config/versions/2',headers=h[b]).json()['body']['metric']=='mrr'


def test_delete_tombstone_and_pagination(team):
    c, _, w, a, b, _, h=team
    root=f'/v1/workspaces/{w}'
    c.post(root+'/mutations',headers=h[a],json=mutation('a'))
    c.post(root+'/mutations',headers=h[a],json=mutation('private',visibility='private'))
    c.post(root+'/mutations',headers=h[a],json=mutation('a',1,deleted=True))
    cursor=0; events=[]
    while True:
        page=c.get(root+'/changes',headers=h[b],params={'after':cursor,'epoch':1,'limit':1}).json()
        events.extend(page['changes']);assert page['cursor']>cursor;cursor=page['cursor']
        if not page['has_more']:break
    assert [e['op'] for e in events]==['upsert','delete']
    assert c.get(root+'/snapshot',headers=h[b]).json()['records']==[]


def test_postgres_delayed_transaction_cannot_be_overtaken(team):
    c, app, w, a, b, _, h=team
    engine=app.state.engine
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row-lock acceptance test')
    started=threading.Event()
    def hold_lock():
        with engine.begin() as db:
            db.execute(select(workspaces).where(workspaces.c.id==w).with_for_update()).first()
            started.set()
            time.sleep(.35)
    def write():
        started.wait(2)
        return c.post(f'/v1/workspaces/{w}/mutations',headers=h[a],json=mutation('ordered'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(hold_lock);second=pool.submit(write)
        assert started.wait(2)
        time.sleep(.1)
        assert not second.done(), 'writer bypassed workspace transaction lock'
        first.result();assert second.result().json()['cursor']==1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda n:c.post(f'/v1/workspaces/{w}/mutations',headers=h[b],json=mutation(f'parallel-{n}')),range(8)))
    assert all(r.status_code==200 for r in results)
    assert sorted(r.json()['cursor'] for r in results)==list(range(2,10))
    snapshot=c.get(f'/v1/workspaces/{w}/snapshot',headers=h[a]).json()
    assert snapshot['cursor']==9 and len(snapshot['records'])==9


def test_private_revision_cannot_be_published_indirectly(team):
    c, _, w, a, b, _, h=team
    root=f'/v1/workspaces/{w}'
    c.post(root+'/mutations', headers=h[a], json=mutation('draft', visibility='private'))
    c.post(root+'/mutations', headers=h[a], json=mutation('draft', 1, 'workspace'))
    snap=mutation('snapshot');snap['kind']='research_snapshot'
    snap['body']={'pins':[{'record_id':'draft','revision':1}]}
    assert c.post(root+'/mutations', headers=h[a], json=snap).status_code==422
    assert c.get(root+'/records/draft/versions/1',headers=h[b]).status_code==404
    snap['body']={'pins':[{'record_id':'draft','revision':2}]}
    assert c.post(root+'/mutations', headers=h[a], json=snap).status_code==200
    assert c.get(root+'/records/draft/versions/2',headers=h[b]).status_code==200
