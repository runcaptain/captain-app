import uuid

from test_server import team, mutation


def setup_job(team, private=False):
    c, _, w, a, b, _, h=team
    root=f'/v1/workspaces/{w}'
    visibility='private' if private else 'workspace'
    assert c.post(root+'/mutations',headers=h[a],json=mutation('config',visibility=visibility)).status_code==200
    snapshot=mutation('snapshot',visibility=visibility)
    snapshot.update(kind='research_snapshot',body={'pins':[{'record_id':'config','revision':1}]})
    assert c.post(root+'/mutations',headers=h[a],json=snapshot).status_code==200
    submit={'mutation_id':uuid.uuid4().hex,'job_id':'job','snapshot_id':'snapshot','snapshot_revision':1,'visibility':visibility}
    result=c.post(root+'/jobs',headers=h[a],json=submit)
    assert result.status_code==200,result.text
    return root,submit,result.json()


def attempt(runner, revision=1):
    return {'mutation_id':uuid.uuid4().hex,'runner_id':runner,'expected_revision':revision}


def event(state='running', sequence=1, attempt=1, progress=.2):
    return {'mutation_id':uuid.uuid4().hex,'attempt':attempt,'sequence':sequence,'state':state,'progress':progress}


def job(c,root,header):
    return next(r for r in c.get(root+'/snapshot',headers=header).json()['records'] if r['id']=='job')


def test_submission_retry_pins_and_generic_bypass(team):
    c,_,_,a,b,_,h=team
    root,submit,ack=setup_job(team)
    assert c.post(root+'/jobs',headers=h[a],json=submit).json()==ack
    assert len([r for r in c.get(root+'/snapshot',headers=h[b]).json()['records'] if r['kind']=='job'])==1
    reuse=dict(submit,snapshot_revision=2)
    assert c.post(root+'/jobs',headers=h[a],json=reuse).status_code==409
    body=job(c,root,h[a])['body']
    assert body['snapshot_id']=='snapshot' and body['snapshot_revision']==1 and body['state']=='queued'
    malicious=mutation('job',1);malicious['kind']='job';malicious['body']={'state':'succeeded'}
    assert c.post(root+'/mutations',headers=h[a],json=malicious).status_code==422


def test_assigned_runner_fencing_terminal_and_progress(team):
    c,_,_,a,b,outsider,h=team
    root,_,_=setup_job(team)
    assert c.post(root+'/jobs/job/attempts',headers=h[b],json=attempt(b)).status_code==403
    start=c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b))
    assert start.status_code==200
    assert c.post(root+'/jobs/job/events',headers=h[a],json=event()).status_code==403
    assert c.post(root+'/jobs/job/events',headers=h[outsider],json=event()).status_code==403
    first=event(progress=.6)
    ack=c.post(root+'/jobs/job/events',headers=h[b],json=first)
    assert ack.status_code==200
    assert c.post(root+'/jobs/job/events',headers=h[b],json=first).json()==ack.json()
    assert c.post(root+'/jobs/job/events',headers=h[b],json=event(sequence=2,progress=.5)).status_code==409
    restart=c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b,3))
    assert restart.status_code==200
    assert c.post(root+'/jobs/job/events',headers=h[b],json=event('succeeded',sequence=2,progress=1)).status_code==409
    finish=event('succeeded',attempt=2,progress=1)
    assert c.post(root+'/jobs/job/events',headers=h[b],json=finish).status_code==200
    assert c.post(root+'/jobs/job/events',headers=h[b],json=event(attempt=2,sequence=2,progress=1)).status_code==409
    assert job(c,root,h[a])['body']['state']=='succeeded'
    assert c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b,5)).status_code==409


def test_cancel_intent_is_distinct_from_confirmation(team):
    c,_,_,a,b,_,h=team
    root,_,_=setup_job(team)
    c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b))
    assert c.post(root+'/jobs/job/events',headers=h[b],json=event('cancelled')).status_code==409
    cancel={'mutation_id':uuid.uuid4().hex,'expected_revision':2}
    assert c.post(root+'/jobs/job/cancel',headers=h[a],json=cancel).status_code==200
    pending=job(c,root,h[b])['body']
    assert pending['state']=='running' and pending['cancel_requested'] is True
    assert c.post(root+'/jobs/job/events',headers=h[b],json=event('cancelled')).status_code==200
    final=job(c,root,h[a])['body']
    assert final['state']=='cancelled' and final['cancel_requested'] is True


def test_private_job_and_revoked_runner(team):
    c,_,_,a,b,_,h=team
    root,_,_=setup_job(team,private=True)
    assert c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b)).status_code==422
    assert c.post(root+'/jobs/job/attempts',headers=h[b],json=attempt(b)).status_code==404
    assert c.post(root+'/jobs/job/events',headers=h[b],json=event()).status_code==404
    c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(a))
    c.put(root+f'/members/{a}',headers=h[a],json={'role':'revoked'})
    assert c.post(root+'/jobs/job/events',headers=h[a],json=event()).status_code==403


def test_queued_cancel_is_confirmed_without_starting_work(team):
    c,_,_,a,b,_,h=team
    root,_,_=setup_job(team)
    cancel={'mutation_id':uuid.uuid4().hex,'expected_revision':1}
    assert c.post(root+'/jobs/job/cancel',headers=h[a],json=cancel).status_code==200
    assert job(c,root,h[a])['body']['state']=='cancelled'
    assert c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b,2)).status_code==409


def test_runner_cannot_start_after_pin_access_is_withdrawn(team):
    c,_,_,a,b,_,h=team
    root,_,_=setup_job(team)
    assert c.post(root+'/mutations',headers=h[a],json=mutation('config',1,'private')).status_code==200
    assert c.post(root+'/jobs/job/attempts',headers=h[a],json=attempt(b)).status_code==422
    submit={'mutation_id':uuid.uuid4().hex,'job_id':'another-job','snapshot_id':'snapshot','snapshot_revision':1,'visibility':'workspace'}
    assert c.post(root+'/jobs',headers=h[b],json=submit).status_code==422
    assert c.post(root+'/jobs',headers=h[a],json=submit).status_code==422
