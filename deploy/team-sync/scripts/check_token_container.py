"""Isolated production-token runtime, operator CLI and hash-only persistence check."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
import uuid

from check_container import docker, wait_ready


def main():
    image = os.environ.get('CST_CHECK_IMAGE', 'captain-team-sync:token-check')
    name = 'cst-token-check-' + uuid.uuid4().hex[:10]
    pg, api = name + '-pg', name + '-api'
    with tempfile.TemporaryDirectory(prefix='cst-token-runtime-') as temp:
        root = Path(temp)
        env = root / 'server.env'
        env.write_text('\n'.join([
            f'CST_TEAM_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@{pg}/synthetic',
            'CST_TEAM_ARTIFACT_ROOT=/artifacts', 'CST_TEAM_AUTH_MODE=tokens',
            'PYTHONDONTWRITEBYTECODE=1']))
        env.chmod(0o600)
        def operator(*args):
            result = docker('run', '--rm', '--network', name, '--user', f'{os.getuid()}:{os.getgid()}',
                            '--env-file', str(env), '-v', str(root) + ':/output', image,
                            'cst', 'team-server', *args)
            return result.stdout.strip()
        def issue(user):
            info = json.loads(operator('token', 'issue', user, '--output', f'/output/{user}.token'))
            path = root / (user + '.token')
            assert path.stat().st_mode & 0o077 == 0
            return info, path.read_text().strip()
        try:
            docker('network', 'create', name)
            docker('volume', 'create', name)
            docker('run', '-d', '--name', pg, '--network', name,
                   '-e', 'POSTGRES_USER=synthetic', '-e', 'POSTGRES_PASSWORD=synthetic',
                   '-e', 'POSTGRES_DB=synthetic', 'postgres:17')
            wait_ready(lambda: docker('exec', pg, 'pg_isready', '-U', 'synthetic', check=False).returncode == 0, 'PostgreSQL')
            operator('migrate')
            for user, role in [('alice', 'admin'), ('bob', 'editor'), ('charlie', 'reader')]:
                operator('grant', user, 'team', '--role', role)
            alice, alice_token = issue('alice')
            bob, bob_token = issue('bob')
            charlie, charlie_token = issue('charlie')
            docker('run', '-d', '--name', api, '--network', name, '--read-only', '--tmpfs', '/tmp',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '-p', '127.0.0.1::8000', '--env-file', str(env), '-v', name + ':/artifacts', image)
            port = docker('port', api, '8000/tcp').stdout.strip().rsplit(':', 1)[1]
            origin = 'http://127.0.0.1:' + port
            def request(path, token=alice_token, body=None, method=None, expected=200):
                req = urllib.request.Request(origin + path,
                    data=json.dumps(body).encode() if body is not None else None, method=method,
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                try:
                    response = urllib.request.urlopen(req, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    assert response.status == expected, (path, response.status, expected)
                    return json.loads(response.read())
            wait_ready(lambda: request('/health') is not None, 'token API')
            base = '/v1/workspaces/team'
            assert request(base + '/identity')['user_id'] == 'alice'
            request(base + '/snapshot', token='synthetic-wrong-token', expected=401)
            mutation = dict(mutation_id=uuid.uuid4().hex, record_id='shared', kind='config',
                            expected_revision=0, visibility='workspace', body={'synthetic': True})
            request(base + '/mutations', body=mutation)
            request(base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex,
                    record_id='private', visibility='private'))
            assert len(request(base + '/snapshot', token=bob_token)['records']) == 1
            request(base + '/mutations', token=charlie_token,
                    body=dict(mutation, mutation_id=uuid.uuid4().hex, record_id='reader'), expected=403)
            rotated = json.loads(operator('token', 'rotate', alice['id'], '--output', '/output/rotated.token'))
            rotated_token = (root / 'rotated.token').read_text().strip()
            request(base + '/snapshot', expected=401)
            assert request(base + '/identity', token=rotated_token)['user_id'] == 'alice'
            assert len(request(base + '/snapshot', token=rotated_token)['records']) == 2
            operator('token', 'revoke', bob['id'])
            request(base + '/snapshot', token=bob_token, expected=401)
            operator('grant', 'bob', 'team', '--role', 'editor')
            request(base + '/snapshot', token=bob_token, expected=401)
            # Expire only a synthetic token in this dedicated database.
            docker('exec', pg, 'psql', '-U', 'synthetic', '-d', 'synthetic', '-c',
                   "UPDATE sync_access_tokens SET expires_at=0 WHERE id='" + charlie['id'] + "'")
            request(base + '/snapshot', token=charlie_token, expected=401)
            listed = operator('token', 'list')
            dump = docker('exec', pg, 'pg_dump', '-U', 'synthetic', 'synthetic').stdout
            logs = docker('logs', api).stdout + docker('logs', api).stderr
            for value in (alice_token, bob_token, charlie_token, rotated_token):
                if value in listed or value in dump or value in logs:
                    raise AssertionError('Raw token leaked into metadata, database or logs')
                if hashlib.sha256(value.encode()).hexdigest() in listed:
                    raise AssertionError('Token list exposed a credential hash')
            docker('restart', api)
            port = docker('port', api, '8000/tcp').stdout.strip().rsplit(':', 1)[1]
            origin = 'http://127.0.0.1:' + port
            wait_ready(lambda: request('/health', token=rotated_token) is not None, 'restarted token API')
            assert len(request(base + '/snapshot', token=rotated_token)['records']) == 2
            operator('revoke', 'team', 'alice')
            request(base + '/snapshot', token=rotated_token, expected=403)
            print('PASS: production token mode, operator issue/list/revoke/rotate, expiry, permissions, restart and hash-only DB')
        finally:
            for container in (api, pg):
                docker('rm', '-f', container, check=False)
                assert docker('container', 'inspect', container, check=False).returncode != 0
            docker('volume', 'rm', name, check=False)
            docker('network', 'rm', name, check=False)
            for kind in ('volume', 'network'):
                assert docker(kind, 'inspect', name, check=False).returncode != 0
            print('CLEANUP VERIFIED: owned token-runtime containers, volume and network removed')


if __name__ == '__main__':
    main()
