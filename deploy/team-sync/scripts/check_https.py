"""Real loopback TLS + token onboarding using the shipped Compose/Caddy overlays."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import uuid

from check_container import docker, wait_ready


def main():
    source = Path(__file__).resolve().parents[1]
    project = 'cst-https-' + uuid.uuid4().hex[:10]
    image = os.environ.get('CST_CHECK_IMAGE', 'captain-team-sync:token-check')
    with tempfile.TemporaryDirectory(prefix='cst-https-') as temp:
        root = Path(temp)
        for filename in ('compose.yaml', 'compose.postgres.yaml', 'compose.https.yaml',
                         'compose.https-local.yaml', 'Caddyfile', 'Caddyfile.common', 'Caddyfile.local'):
            shutil.copyfile(source / filename, root / filename)
        env = root / 'server.env'
        env.write_text('\n'.join([
            'CST_TEAM_DATABASE_URL=postgresql+psycopg://captain:synthetic@postgres/captain_team',
            'CST_TEAM_POSTGRES_PASSWORD=synthetic', 'CST_TEAM_AUTH_MODE=tokens',
            'CST_TEAM_HOSTNAME=sync.test', 'CST_TEAM_LOCAL_HTTPS_PORT=0']))
        env.chmod(0o600)
        override = root / 'compose.check.yaml'
        override.write_text('services:\n  migrate:\n    image: ' + image + '\n  api:\n    image: ' + image +
                            '\n    ports: !reset []\n    healthcheck:\n      interval: 1s\n  caddy:\n    healthcheck:\n      interval: 1s\n')
        base = ['docker', 'compose', '-p', project, '--env-file', str(env)]
        for filename in ('compose.yaml', 'compose.postgres.yaml', 'compose.https.yaml',
                         'compose.https-local.yaml', 'compose.check.yaml'):
            base += ['-f', str(root / filename)]
        base += ['--profile', 'https']
        check_env = dict(os.environ, CST_TEAM_POSTGRES_PASSWORD='synthetic',
                         CST_TEAM_HOSTNAME='sync.test', CST_TEAM_LOCAL_HTTPS_PORT='0',
                         CST_TEAM_HTTPS_BIND='127.0.0.1', CST_TEAM_HTTP_PORT='0', CST_TEAM_HTTPS_PORT='0')
        def compose(*args, check=True):
            return subprocess.run([*base, *args], check=check, capture_output=True, text=True, env=check_env)
        def operator(*args):
            return compose('run', '--rm', '--no-deps', '--user', f'{os.getuid()}:{os.getgid()}',
                           '-v', str(root) + ':/output', 'migrate', 'cst', 'team-server', *args).stdout.strip()
        def port():
            result = compose('port', 'caddy', '443').stdout.strip()
            assert result.startswith('127.0.0.1:'), 'TLS test must remain loopback-only'
            return int(result.rsplit(':', 1)[1])
        try:
            compose('config', '--quiet')
            compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '120')
            exposed_port = port()
            ca_path = root / 'client-test-ca.crt'
            wait_ready(lambda: compose('cp', 'caddy:/data/caddy/pki/authorities/local/root.crt',
                                       str(ca_path), check=False).returncode == 0, 'local test CA')
            original_ca = ca_path.read_bytes()
            context = ssl.create_default_context(cafile=str(ca_path))
            class LocalHTTPS(http.client.HTTPSConnection):
                def connect(self):
                    # Local client routing only. No DNS or host trust store changes.
                    raw = socket.create_connection(('127.0.0.1', exposed_port), timeout=10)
                    try:
                        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                    except BaseException:
                        raw.close()
                        raise
            def request(path, token=None, body=None, method='GET', expected=200, binary=False):
                headers = {}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                    headers['Content-Type'] = 'application/json'
                connection = LocalHTTPS('sync.test', exposed_port, context=context, timeout=10)
                try:
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
                    payload = response.read()
                    assert response.status == expected, (path, response.status, expected)
                    return payload if binary else json.loads(payload)
                finally:
                    connection.close()
            for hostname, trust in [('sync.test', ssl.create_default_context()), ('wrong.test', context)]:
                connection = LocalHTTPS(hostname, exposed_port, context=trust, timeout=10)
                try:
                    connection.connect()
                except ssl.SSLCertVerificationError:
                    pass
                except ssl.SSLError:
                    # Caddy can refuse an unknown SNI before presenting a cert.
                    if hostname == 'sync.test':
                        raise
                else:
                    raise AssertionError('TLS accepted an untrusted CA or wrong hostname')
                finally:
                    connection.close()
            assert request('/health')['ok']
            permissions = compose('exec', '-T', 'caddy', 'stat', '-c', '%a',
                                  '/data/caddy/pki/authorities/local/root.key').stdout.strip()
            assert int(permissions, 8) & 0o077 == 0, 'Local CA private key is not private'
            issued = {}
            credentials = {}
            for user, role in [('alice', 'admin'), ('bob', 'editor')]:
                operator('grant', user, 'team', '--role', role)
                issued[user] = json.loads(operator('token', 'issue', user, '--output', '/output/' + user + '.token'))
                file = root / (user + '.token')
                assert file.stat().st_mode & 0o077 == 0
                credentials[user] = file.read_text().strip()
            alice, bob = credentials['alice'], credentials['bob']
            path = '/v1/workspaces/team'
            request(path + '/identity', expected=401)
            request(path + '/identity', token='wrong-token', expected=401)
            assert request(path + '/identity', token=alice)['user_id'] == 'alice'
            for identifier, visibility in [('shared', 'workspace'), ('private', 'private')]:
                request(path + '/mutations', token=alice, method='POST', body={
                    'mutation_id': uuid.uuid4().hex, 'record_id': identifier, 'kind': 'config',
                    'expected_revision': 0, 'visibility': visibility, 'body': {'synthetic': True}})
            assert [r['id'] for r in request(path + '/snapshot', token=bob)['records']] == ['shared']
            content = b't' * (8 * 1024 * 1024)
            upload = request(path + '/artifacts/uploads', token=alice, method='POST', body={
                'mutation_id': uuid.uuid4().hex, 'artifact_id': 'tls-artifact', 'visibility': 'workspace',
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
            endpoint = path + '/artifacts/uploads/' + upload['upload_id']
            request(endpoint, token=alice, method='PUT', body=content)
            request(endpoint + '/finalize', token=alice, method='POST', body={'mutation_id': uuid.uuid4().hex})
            assert request(path + '/artifacts/tls-artifact/content', token=bob, binary=True) == content
            rotated = json.loads(operator('token', 'rotate', issued['alice']['id'], '--output', '/output/rotated.token'))
            replacement = (root / 'rotated.token').read_text().strip()
            request(path + '/identity', token=alice, expected=401)
            assert request(path + '/identity', token=replacement)['user_id'] == 'alice'
            operator('token', 'revoke', issued['bob']['id'])
            request(path + '/snapshot', token=bob, expected=401)
            compose('restart', 'caddy')
            exposed_port = port()
            wait_ready(lambda: request('/health')['ok'], 'restarted TLS proxy')
            compose('cp', 'caddy:/data/caddy/pki/authorities/local/root.crt', str(ca_path))
            assert ca_path.read_bytes() == original_ca, 'Restart replaced persistent CA state'
            assert request(path + '/identity', token=replacement)['user_id'] == 'alice'
            compose('stop', 'postgres')
            failed_health = compose('exec', '-T', 'caddy', 'wget', '-q', '-O', '/dev/null',
                                    'http://127.0.0.1:2018/health', check=False)
            assert failed_health.returncode != 0, 'Proxy readiness ignored database outage'
            compose('start', 'postgres')
            wait_ready(lambda: compose('exec', '-T', 'postgres', 'pg_isready', '-U', 'captain', check=False).returncode == 0, 'restarted database')
            wait_ready(lambda: request('/health')['ok'], 'restored readiness')
            logs = compose('logs', '--no-color').stdout
            for value in (*credentials.values(), replacement):
                if value in logs:
                    raise AssertionError('Token leaked into service logs')
            print('PASS: shipped HTTPS Compose, default trust rejected, custom CA verified, hostname checks, token onboarding/ACL/rotation/revoke, 8 MiB artifact, persistent CA, readiness outage/recovery')
            print('TRUST: temporary client SSLContext only; no Mac trust store, hosts file, DNS or public listener changes')
        finally:
            compose('down', '-v', '--remove-orphans', check=False)
            for kind, args in [('container', ('ps', '-aq')), ('volume', ('volume', 'ls', '-q')), ('network', ('network', 'ls', '-q'))]:
                remaining = docker(*args, '--filter', 'label=com.docker.compose.project=' + project).stdout.strip()
                assert not remaining, 'Owned HTTPS ' + kind + ' cleanup incomplete'
            print('CLEANUP VERIFIED: owned Compose containers, certificate/data volumes and network removed')


if __name__ == '__main__':
    main()
