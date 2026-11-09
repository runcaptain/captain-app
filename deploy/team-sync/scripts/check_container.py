"""Synthetic packaged runtime check. Removes only resources created by this run."""
import json
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


def wait_ready(check, label):
    for _ in range(90):
        try:
            if check():
                return
        except (OSError, urllib.error.URLError, subprocess.CalledProcessError):
            pass
        time.sleep(1)
    raise RuntimeError(label + ' did not become ready')


def main():
    image = os.environ.get('CST_CHECK_IMAGE', 'captain-team-sync:synthetic-check')
    name = 'cst-check-' + uuid.uuid4().hex[:10]
    pg, api = name + '-pg', name + '-api'
    tokens = {user: secrets.token_urlsafe(24) for user in ('alice', 'bob')}
    with tempfile.TemporaryDirectory(prefix='cst-container-') as temp:
        root = Path(temp)
        fixture = root / 'fixture.json'
        fixture.write_text(json.dumps({'users': [{'id': u, 'token': t} for u, t in tokens.items()],
                                      'workspaces': [{'id': 'team', 'members': {'alice': 'admin', 'bob': 'editor'}}]}))
        # Container's unprivileged user reads a dedicated synthetic fixture only.
        root.chmod(0o755)
        fixture.chmod(0o644)
        env = root / 'server.env'
        env.write_text('\n'.join([
            f'CST_TEAM_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@{pg}/synthetic',
            'CST_TEAM_ARTIFACT_ROOT=/artifacts', 'CST_TEAM_DEV_TOKENS=synthetic-only',
            'CST_TEAM_DEV_FIXTURE=/fixture.json', 'PYTHONDONTWRITEBYTECODE=1']))
        env.chmod(0o600)
        try:
            docker('network', 'create', name)
            docker('volume', 'create', name)
            docker('run', '-d', '--name', pg, '--network', name,
                   '-e', 'POSTGRES_USER=synthetic', '-e', 'POSTGRES_PASSWORD=synthetic',
                   '-e', 'POSTGRES_DB=synthetic', 'postgres:17')
            wait_ready(lambda: docker('exec', pg, 'pg_isready', '-U', 'synthetic', check=False).returncode == 0, 'PostgreSQL')
            docker('run', '-d', '--name', api, '--network', name, '--read-only',
                   '--tmpfs', '/tmp', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '-p', '127.0.0.1::8000', '--env-file', str(env),
                   '-v', str(fixture) + ':/fixture.json:ro', '-v', name + ':/artifacts', image)
            port = docker('port', api, '8000/tcp').stdout.strip().rsplit(':', 1)[1]
            origin = 'http://127.0.0.1:' + port

            def request(path, user='alice', body=None, method=None, expected=200):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(origin + path, data=data, method=method,
                    headers={'Authorization': 'Bearer ' + tokens[user], 'Content-Type': 'application/json'})
                try:
                    response = urllib.request.urlopen(req, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    assert response.status == expected, (path, response.status, expected)
                    return json.loads(response.read())

            wait_ready(lambda: request('/health') is not None, 'API')
            base = '/v1/workspaces/team'
            mutation = dict(mutation_id=uuid.uuid4().hex, record_id='shared', kind='config',
                            expected_revision=0, visibility='workspace', body={'synthetic': True})
            request(base + '/mutations', body=mutation)
            assert len(request(base + '/snapshot', user='bob')['records']) == 1
            request(base + '/mutations', user='bob', body=dict(mutation, mutation_id=uuid.uuid4().hex), expected=409)
            request(base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex,
                    record_id='private', visibility='private'))
            assert len(request(base + '/snapshot', user='bob')['records']) == 1
            content = b'synthetic container artifact'
            upload = request(base + '/artifacts/uploads', body={
                'mutation_id': uuid.uuid4().hex, 'artifact_id': 'artifact',
                'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content),
                'visibility': 'workspace'})
            endpoint = base + '/artifacts/uploads/' + upload['upload_id']
            upload_request = urllib.request.Request(origin + endpoint, data=content, method='PUT',
                headers={'Authorization': 'Bearer ' + tokens['alice']})
            with urllib.request.urlopen(upload_request, timeout=5) as response:
                assert response.status == 200
            request(endpoint + '/finalize', body={'mutation_id': uuid.uuid4().hex})
            docker('restart', api)
            port = docker('port', api, '8000/tcp').stdout.strip().rsplit(':', 1)[1]
            origin = 'http://127.0.0.1:' + port
            wait_ready(lambda: request('/health') is not None, 'restarted API')
            assert len(request(base + '/snapshot')['records']) == 3
            download = urllib.request.Request(origin + base + '/artifacts/artifact/content',
                headers={'Authorization': 'Bearer ' + tokens['bob']})
            with urllib.request.urlopen(download, timeout=5) as response:
                assert response.read() == content
            request(base + '/members/bob', method='PUT', body={'role': 'revoked'})
            request(base + '/snapshot', user='bob', expected=403)
            print('PASS: packaged container, PostgreSQL 17, two users, private records, conflict, artifact bytes, restart persistence, revocation')
        finally:
            for container in (api, pg):
                docker('rm', '-f', container, check=False)
            docker('volume', 'rm', name, check=False)
            docker('network', 'rm', name, check=False)
            for kind in ('container', 'volume', 'network'):
                assert docker(kind, 'inspect', name if kind != 'container' else api, check=False).returncode != 0
            assert docker('container', 'inspect', pg, check=False).returncode != 0
            print('CLEANUP VERIFIED: owned containers, volume and network removed')


if __name__ == '__main__':
    main()
