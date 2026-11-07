import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from captain_search.collaboration.identity import OIDCSettings, OIDCVerifier
from captain_search.collaboration.server import create_app, identities, members, users, workspaces


@pytest.fixture
def identity(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = OIDCSettings('https://identity.example/', 'captain-team', 'https://identity.example/keys')
    monkeypatch.setattr(jwt.PyJWKClient, 'get_signing_key_from_jwt', lambda self, token: SimpleNamespace(key=key.public_key()))
    def token(**overrides):
        claims = dict(iss=settings.issuer, aud=settings.audience, sub='subject', iat=int(time.time()), exp=int(time.time()) + 300)
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'synthetic'})
    return settings, token


@pytest.mark.parametrize('overrides', [{'iss': 'https://wrong.example/'}, {'aud': 'wrong'}, {'exp': 1}, {'sub': ''}, {'iat': int(time.time()) + 3600}])
def test_invalid_claims(identity, overrides):
    settings, token = identity
    with pytest.raises(Exception) as error:
        OIDCVerifier(settings).verify(token(**overrides))
    assert error.value.status_code == 401


def test_algorithm_and_signature_rejected(identity):
    settings, token = identity
    for bad in (jwt.encode(dict(sub='subject'), 'x'*32, algorithm='HS256', headers={'kid':'synthetic'}), token()[:-5] + 'xxxxx'):
        with pytest.raises(Exception) as error:
            OIDCVerifier(settings).verify(bad)
        assert error.value.status_code == 401


def test_membership_revocation_and_no_claim_enrollment(identity, tmp_path):
    settings, token = identity
    from sqlalchemy import create_engine
    from captain_search.collaboration.migrations import migrate
    url = 'sqlite:///' + str(tmp_path / 'oidc.db')
    engine = create_engine(url)
    migrate(engine)
    engine.dispose()
    app = create_app(url, oidc=settings)
    with app.state.engine.begin() as db:
        db.execute(insert(users).values(id='alice', token_hash='disabled'))
        db.execute(insert(identities).values(issuer=settings.issuer, subject='subject', user_id='alice'))
        db.execute(insert(workspaces).values(id='team', cursor=0, epoch=1))
        db.execute(insert(members).values(workspace='team', user_id='alice', role='editor'))
    with TestClient(app) as c:
        headers = {'Authorization': 'Bearer ' + token()}
        path = '/v1/workspaces/team/snapshot'
        assert c.get(path, headers=headers).status_code == 200
        assert c.get(path, headers={'Authorization':'Bearer '+token(sub='unknown')}).status_code == 401
        with app.state.engine.begin() as db:
            db.execute(update(members).values(role='revoked'))
        assert c.get(path, headers=headers).status_code == 403


def test_no_implicit_dev_auth():
    with pytest.raises(ValueError):
        create_app('sqlite:///:memory:')


def test_jwks_failure_fails_closed(identity, monkeypatch):
    settings, token = identity
    def unavailable(self, token):
        raise jwt.PyJWKClientConnectionError('synthetic outage')
    monkeypatch.setattr(jwt.PyJWKClient, 'get_signing_key_from_jwt', unavailable)
    with pytest.raises(Exception) as error:
        OIDCVerifier(settings).verify(token())
    assert error.value.status_code == 401


def test_configuration_requires_https():
    with pytest.raises(ValueError):
        OIDCSettings('http://issuer.example', 'audience', 'https://issuer.example/keys')


def test_auth_modes_cannot_mix(identity):
    settings, _ = identity
    with pytest.raises(ValueError):
        create_app('sqlite:///:memory:', oidc=settings, allow_dev_tokens=True)


def test_operator_enrollment_revocation_regrant_live_api(identity, tmp_path):
    from sqlalchemy import create_engine
    from captain_search.collaboration.migrations import migrate
    from captain_search.collaboration.operator import enroll, revoke
    settings, token = identity
    url = 'sqlite:///' + str(tmp_path / 'operator-api.db')
    engine = create_engine(url)
    migrate(engine)
    args = dict(issuer=settings.issuer, subject='subject', user_id='alice', workspace='team', role='admin')
    enroll(engine, **args)
    service = create_app(url, oidc=settings, artifact_root=tmp_path / 'artifacts')
    with TestClient(service) as client:
        headers = {'Authorization': 'Bearer ' + token()}
        path = '/v1/workspaces/team/snapshot'
        original = client.get(path, headers=headers)
        assert original.status_code == 200
        epoch = original.json()['epoch']
        revoke(engine, workspace='team', user_id='alice')
        assert client.get(path, headers=headers).status_code == 403
        enroll(engine, **args)
        assert client.get(path, headers=headers).status_code == 200
        delta = client.get('/v1/workspaces/team/changes', params={'after':0,'epoch':epoch}, headers=headers)
        assert delta.status_code == 409
        assert delta.json()['detail']['code'] == 'reset_required'
    engine.dispose()


def test_token_cannot_select_jwks_or_discover_an_issuer(monkeypatch):
    import json
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    configured = 'https://operator.example/fixed-jwks'
    settings = OIDCSettings('https://operator.example/', 'team-api', configured)
    fetched = []
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='trusted', use='sig', alg='RS256')
    def fetch(client):
        fetched.append(client.uri)
        return {'keys': [jwk]}
    monkeypatch.setattr(jwt.PyJWKClient, 'fetch_data', fetch)
    claims = {'iss': settings.issuer, 'aud': settings.audience, 'sub': 'member',
              'iat': int(time.time()), 'exp': int(time.time()) + 300}
    headers = {'kid': 'trusted', 'jku': 'https://attacker.invalid/keys',
               'x5u': 'https://attacker.invalid/certificate'}
    verifier = OIDCVerifier(settings)
    token = jwt.encode(claims, key, algorithm='RS256', headers=headers)
    assert verifier.verify(token) == (settings.issuer, 'member')
    claims['iss'] = 'https://attacker.invalid/'
    with pytest.raises(Exception) as error:
        verifier.verify(jwt.encode(claims, key, algorithm='RS256', headers=headers))
    assert error.value.status_code == 401
    assert fetched and set(fetched) == {configured}
