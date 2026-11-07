import hashlib
import uuid

import pytest

from test_server import team, mutation

DATA=b'synthetic ranking output\nquery,item,score\nq1,p1,0.9\n'


def begin(c,root,header,artifact='scores',visibility='workspace',data=DATA):
    request={'mutation_id':uuid.uuid4().hex,'artifact_id':artifact,'sha256':hashlib.sha256(data).hexdigest(),
             'size':len(data),'visibility':visibility}
    response=c.post(root+'/artifacts/uploads',headers=header,json=request)
    assert response.status_code==200,response.text
    return response.json()['upload_id'],request


def test_publish_only_after_verified_upload_and_retry(team):
    c,_,w,a,b,_,h=team
    root=f'/v1/workspaces/{w}'
    upload,request=begin(c,root,h[a])
    assert c.post(root+'/artifacts/uploads',headers=h[a],json=request).json()['upload_id']==upload
    assert c.get(root+'/snapshot',headers=h[b]).json()['records']==[]
    finalize={'mutation_id':uuid.uuid4().hex}
    assert c.post(root+f'/artifacts/uploads/{upload}/finalize',headers=h[a],json=finalize).status_code==409
    assert c.put(root+f'/artifacts/uploads/{upload}',headers=h[a],content=DATA).status_code==200
    assert c.get(root+'/snapshot',headers=h[b]).json()['records']==[]
    ack=c.post(root+f'/artifacts/uploads/{upload}/finalize',headers=h[a],json=finalize)
    assert ack.status_code==200,ack.text
    assert c.post(root+f'/artifacts/uploads/{upload}/finalize',headers=h[a],json=finalize).json()==ack.json()
    record=c.get(root+'/snapshot',headers=h[b]).json()['records'][0]
    assert record['kind']=='artifact' and record['body']['sha256']==hashlib.sha256(DATA).hexdigest()
    downloaded=c.get(root+'/artifacts/scores/content',headers=h[b])
    assert downloaded.content==DATA and downloaded.headers['cache-control']=='no-store'
    assert c.put(root+f'/artifacts/uploads/{upload}',headers=h[a],content=DATA).status_code==409
    malicious=mutation('scores',1);malicious['kind']='artifact';malicious['body']={'sha256':'bad'}
    assert c.post(root+'/mutations',headers=h[a],json=malicious).status_code==422


def test_bad_checksum_truncation_and_oversize_publish_nothing(team):
    c,_,w,a,b,_,h=team
    root=f'/v1/workspaces/{w}'
    upload,_=begin(c,root,h[a])
    endpoint=root+f'/artifacts/uploads/{upload}'
    assert c.put(endpoint,headers=h[a],content=b'x'*len(DATA)).status_code==422
    assert c.put(endpoint,headers=h[a],content=DATA[:-1]).status_code==422
    assert c.put(endpoint,headers=h[a],content=DATA+b'extra').status_code==413
    assert c.post(endpoint+'/finalize',headers=h[a],json={'mutation_id':uuid.uuid4().hex}).status_code==409
    assert c.get(root+'/snapshot',headers=h[b]).json()['records']==[]
    large={'mutation_id':uuid.uuid4().hex,'artifact_id':'huge','sha256':'0'*64,'size':8*1024*1024+1}
    assert c.post(root+'/artifacts/uploads',headers=h[a],json=large).status_code==422


def test_private_artifact_and_revocation(team):
    c,_,w,a,b,outside,h=team
    root=f'/v1/workspaces/{w}'
    upload,_=begin(c,root,h[a],visibility='private')
    endpoint=root+f'/artifacts/uploads/{upload}'
    assert c.put(endpoint,headers=h[b],content=DATA).status_code==404
    assert c.put(endpoint,headers=h[a],content=DATA).status_code==200
    assert c.post(endpoint+'/finalize',headers=h[b],json={'mutation_id':uuid.uuid4().hex}).status_code==404
    assert c.post(endpoint+'/finalize',headers=h[a],json={'mutation_id':uuid.uuid4().hex}).status_code==200
    assert c.get(root+'/artifacts/scores/content',headers=h[b]).status_code==404
    assert c.get(root+'/artifacts/scores/content',headers=h[outside]).status_code==403
    assert c.get(root+'/artifacts/scores/content',headers=h[a]).content==DATA
    c.put(root+f'/members/{a}',headers=h[a],json={'role':'revoked'})
    assert c.get(root+'/artifacts/scores/content',headers=h[a]).status_code==403


def test_tampered_stage_fails_finalize_and_paths_are_opaque(team,tmp_path):
    c,_,w,a,b,_,h=team
    root=f'/v1/workspaces/{w}'
    upload,_=begin(c,root,h[a],artifact='../../escape')
    assert len(upload)==32 and all(x in '0123456789abcdef' for x in upload)
    endpoint=root+f'/artifacts/uploads/{upload}'
    assert c.put(endpoint,headers=h[a],content=DATA).status_code==200
    staged=list((tmp_path/'artifacts'/'staging').iterdir())
    assert len(staged)==1 and staged[0].name.endswith('.upload')
    staged[0].write_bytes(b'corruption')
    response=c.post(endpoint+'/finalize',headers=h[a],json={'mutation_id':uuid.uuid4().hex})
    assert response.status_code==422
    assert str(tmp_path) not in response.text
    assert c.get(root+'/snapshot',headers=h[b]).json()['records']==[]


def test_identical_bytes_reuse_blob_without_overwrite(team,tmp_path):
    c,_,w,a,b,_,h=team
    root=f'/v1/workspaces/{w}'
    for artifact in ('one','two'):
        upload,_=begin(c,root,h[a],artifact=artifact)
        endpoint=root+f'/artifacts/uploads/{upload}'
        c.put(endpoint,headers=h[a],content=DATA)
        assert c.post(endpoint+'/finalize',headers=h[a],json={'mutation_id':uuid.uuid4().hex}).status_code==200
    blobfiles=[p for p in (tmp_path/'artifacts'/'blobs').rglob('*') if p.is_file()]
    assert len(blobfiles)==1 and blobfiles[0].read_bytes()==DATA
    assert len(c.get(root+'/snapshot',headers=h[b]).json()['records'])==2


def test_interrupted_stream_leaves_no_published_metadata(team):
    c,_,w,a,b,_,h=team
    root=f'/v1/workspaces/{w}'
    upload,_=begin(c,root,h[a])
    endpoint=root+f'/artifacts/uploads/{upload}'
    def interrupted():
        yield DATA[:8]
        raise RuntimeError('synthetic_upload_interruption')
    with pytest.raises(RuntimeError,match='synthetic_upload_interruption'):
        c.put(endpoint,headers=h[a],content=interrupted())
    assert c.post(endpoint+'/finalize',headers=h[a],json={'mutation_id':uuid.uuid4().hex}).status_code==409
    assert c.get(root+'/snapshot',headers=h[b]).json()['records']==[]
