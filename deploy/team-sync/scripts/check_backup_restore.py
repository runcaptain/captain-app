"""Isolated real Docker restore proof using production tokens and synthetic data."""
import hashlib
import json
import os
import io
import shutil
import tarfile
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

from check_container import docker, wait_ready


def main():
    image = os.environ.get('CST_CHECK_IMAGE', 'captain-team-sync:token-check')
    name = 'cst-backup-check-' + uuid.uuid4().hex[:10]
    source_pg, restored_pg = name + '-source-pg', name + '-restored-pg'
    source_api, restored_api = name + '-source-api', name + '-restored-api'
    source_volume, restored_volume = name + '-source-files', name + '-restored-files'
    containers = (source_api, restored_api, source_pg, restored_pg)
    volumes = (source_volume, restored_volume)
    helper = Path(__file__).with_name('backup_local.py')
    with tempfile.TemporaryDirectory(prefix='cst-backup-') as temp:
        root = Path(temp)
        os.chmod(root, 0o700)
        def env(pg):
            path = root / (pg + '.env')
            path.write_text('\n'.join([
                f'CST_TEAM_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@{pg}/synthetic',
                'CST_TEAM_ARTIFACT_ROOT=/artifacts', 'CST_TEAM_AUTH_MODE=tokens', 'PYTHONDONTWRITEBYTECODE=1']))
            path.chmod(0o600)
            return path
        source_env, restored_env = env(source_pg), env(restored_pg)
        def operator(*args, pg_env=source_env):
            return docker('run', '--rm', '--network', name, '--user', f'{os.getuid()}:{os.getgid()}',
                          '--env-file', str(pg_env), '-v', str(root) + ':/output', image,
                          'cst', 'team-server', *args).stdout.strip()
        def start_api(container, pg_env, volume):
            docker('run', '-d', '--name', container, '--network', name, '--read-only', '--tmpfs', '/tmp',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '-p', '127.0.0.1::8000', '--env-file', str(pg_env), '-v', volume + ':/artifacts', image)
            return origin(container)
        def origin(container):
            port = docker('port', container, '8000/tcp').stdout.strip().rsplit(':', 1)[1]
            return 'http://127.0.0.1:' + port
        tokens = {}
        def request(url, path, user='alice', body=None, method=None, expected=200, binary=False):
            data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url + path, data=data, method=method,
                headers={'Authorization': 'Bearer ' + tokens.get(user, 'invalid'), 'Content-Type': 'application/json'})
            try:
                response = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                assert response.status == expected, (path, response.status, expected)
                raw = response.read()
                return raw if binary else json.loads(raw)
        def run_helper(action, pg, volume, *args, expected=0):
            result = subprocess.run([sys.executable, str(helper), action, '--postgres', pg, '--user', 'synthetic',
                                     '--database', 'synthetic', '--artifacts', volume, *args], capture_output=True, text=True)
            for token in tokens.values():
                assert token not in result.stdout and token not in result.stderr
            assert result.returncode == expected, 'Backup helper returned unexpected status: ' + result.stderr.strip()
            return result
        try:
            for flag in ('--database', '--user'):
                denied = run_helper('restore', restored_pg, restored_volume, '--input', str(root), flag, 'postgresql://secret@outside/db', expected=1)
                assert 'Invalid PostgreSQL identifier' in denied.stderr
                assert 'secret@outside' not in denied.stderr
            docker('network', 'create', name)
            for volume in volumes:
                docker('volume', 'create', volume)
            for pg in (source_pg, restored_pg):
                docker('run', '-d', '--name', pg, '--network', name,
                       '-e', 'POSTGRES_USER=synthetic', '-e', 'POSTGRES_PASSWORD=synthetic',
                       '-e', 'POSTGRES_DB=synthetic', 'postgres:17')
                wait_ready(lambda pg=pg: docker('exec', pg, 'pg_isready', '-U', 'synthetic', check=False).returncode == 0, 'PostgreSQL')
            operator('migrate')
            ids = {}
            for user, role in [('alice', 'admin'), ('bob', 'reader'), ('revoked', 'reader')]:
                operator('grant', user, 'team', '--role', role)
                ids[user] = json.loads(operator('token', 'issue', user, '--output', '/output/' + user + '.token'))['id']
                tokens[user] = (root / (user + '.token')).read_text().strip()
            operator('token', 'revoke', ids['revoked'])
            source = start_api(source_api, source_env, source_volume)
            wait_ready(lambda: request(source, '/health'), 'source API')
            base = '/v1/workspaces/team'
            mutation = {'mutation_id': uuid.uuid4().hex, 'record_id': 'shared', 'kind': 'config',
                        'expected_revision': 0, 'visibility': 'workspace', 'body': {'synthetic': True}}
            request(source, base + '/mutations', body=mutation)
            request(source, base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex,
                    record_id='private', visibility='private'))
            content = b'synthetic backup artifact\n'
            upload = request(source, base + '/artifacts/uploads', body={
                'mutation_id': uuid.uuid4().hex, 'artifact_id': 'artifact', 'sha256': hashlib.sha256(content).hexdigest(),
                'size': len(content), 'visibility': 'workspace'})
            upload_path = base + '/artifacts/uploads/' + upload['upload_id']
            request(source, upload_path, body=content, method='PUT')
            request(source, upload_path + '/finalize', body={'mutation_id': uuid.uuid4().hex})
            before = request(source, base + '/snapshot')
            assert request(source, base + '/artifacts/artifact/content', user='bob', binary=True) == content
            failed = run_helper('backup', source_pg, source_volume, '--writer', source_api,
                                '--writer', name + '-missing-writer', '--output', str(root / 'failed-backup'), expected=1)
            assert docker('inspect', '-f', '{{.State.Running}}', source_api).stdout.strip() == 'true'
            backup = root / 'backup'
            run_helper('backup', source_pg, source_volume, '--writer', source_api, '--output', str(backup))
            assert docker('inspect', '-f', '{{.State.Running}}', source_api).stdout.strip() == 'true'
            assert backup.stat().st_mode & 0o077 == 0
            assert all(path.stat().st_mode & 0o077 == 0 for path in backup.iterdir())
            # A client advanced beyond this backup before the incident. Its
            # discarded branch must not survive when restored cursors catch up.
            source = origin(source_api)
            wait_ready(lambda: request(source, '/health'), 'source API after backup')
            request(source, base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex,
                    expected_revision=1, body={'branch': 'discarded'}))
            request(source, base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex, record_id='lost-after-backup'))
            stale = request(source, base + '/snapshot')
            assert stale['cursor'] == before['cursor'] + 2
            docker('stop', source_api)
            run_helper('backup', source_pg, source_volume, '--writer', source_api, '--output', str(root / 'stopped-backup'))
            assert docker('inspect', '-f', '{{.State.Running}}', source_api).stdout.strip() == 'false'
            # Corruption and unsafe paths must be rejected before any target DDL.
            unsafe = root / 'unsafe-backup'
            shutil.copytree(backup, unsafe)
            with tarfile.open(unsafe / 'artifacts.tar', 'w') as archive:
                entry = tarfile.TarInfo('../escape')
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b'x'))
            manifest = json.loads((unsafe / 'manifest.json').read_text())
            manifest['files']['artifacts.tar'] = hashlib.sha256((unsafe / 'artifacts.tar').read_bytes()).hexdigest()
            (unsafe / 'manifest.json').write_text(json.dumps(manifest))
            denied = run_helper('restore', restored_pg, restored_volume, '--input', str(unsafe), expected=1)
            assert 'Unsafe artifact archive entry' in denied.stderr
            (unsafe / 'metadata.dump').write_bytes(b'corrupted')
            denied = run_helper('restore', restored_pg, restored_volume, '--input', str(unsafe), expected=1)
            assert 'checksum mismatch' in denied.stderr
            # Restore refuses populated targets, even before clearing any rows.
            docker('exec', restored_pg, 'psql', '-U', 'synthetic', '-d', 'synthetic', '-c', 'CREATE TABLE keep_me (id integer);')
            legacy = root / 'legacy-backup'
            shutil.copytree(backup, legacy)
            legacy_manifest = json.loads((legacy / 'manifest.json').read_text())
            legacy_manifest['format'] = 1
            legacy_manifest.pop('schema_version')
            (legacy / 'manifest.json').write_text(json.dumps(legacy_manifest))
            denied = run_helper('restore', restored_pg, restored_volume, '--input', str(legacy), '--replace-existing', expected=1)
            assert 'target not modified' in denied.stderr
            denied = run_helper('restore', restored_pg, restored_volume, '--input', str(backup), expected=1)
            assert 'not empty' in denied.stderr
            assert docker('exec', restored_pg, 'psql', '-U', 'synthetic', '-d', 'synthetic', '-Atc',
                          "SELECT to_regclass('keep_me')").stdout.strip() == 'keep_me'
            docker('exec', restored_pg, 'psql', '-U', 'synthetic', '-d', 'synthetic', '-c', 'DROP TABLE keep_me;')
            docker('exec', restored_pg, 'psql', '-U', 'synthetic', '-d', 'synthetic', '-c', 'CREATE SCHEMA pgx_private;')
            denied = run_helper('restore', restored_pg, restored_volume, '--input', str(backup), expected=1)
            assert 'unexpected user schema' in denied.stderr
            docker('exec', restored_pg, 'psql', '-U', 'synthetic', '-d', 'synthetic', '-c', 'DROP SCHEMA pgx_private;')
            run_helper('restore', restored_pg, restored_volume, '--input', str(backup))
            restored = start_api(restored_api, restored_env, restored_volume)
            wait_ready(lambda: request(restored, '/health'), 'restored API')
            after = request(restored, base + '/snapshot')
            before['records'].sort(key=lambda row: row['id'])
            after['records'].sort(key=lambda row: row['id'])
            assert after['history_id'] != before['history_id']
            assert {k: v for k, v in before.items() if k != 'history_id'} == {k: v for k, v in after.items() if k != 'history_id'}
            assert request(restored, base + '/identity')['user_id'] == 'alice'
            assert {row['id'] for row in request(restored, base + '/snapshot', user='bob')['records']} == {'shared', 'artifact'}
            request(restored, base + '/mutations', user='bob', body=dict(mutation, mutation_id=uuid.uuid4().hex, record_id='denied'), expected=403)
            request(restored, base + '/snapshot', user='revoked', expected=401)
            assert request(restored, base + '/artifacts/artifact/content', user='bob', binary=True) == content
            # Dump privately to confirm hashes survived and plaintext tokens did not.
            dump = docker('exec', restored_pg, 'pg_dump', '-U', 'synthetic', 'synthetic').stdout
            for token in tokens.values():
                assert token not in dump
                assert hashlib.sha256(token.encode()).hexdigest() in dump
            previous_history = stale['history_id']
            histories = {previous_history}
            for cycle in range(2):
                if cycle:
                    docker('stop', restored_api)
                    run_helper('restore', restored_pg, restored_volume, '--input', str(backup), '--replace-existing')
                    docker('start', restored_api)
                    restored = origin(restored_api)
                    wait_ready(lambda: request(restored, '/health'), 'repeated restore API')
                current = request(restored, base + '/snapshot')
                history = current['history_id']
                assert history not in histories, 'Repeated restores must never reuse archived history'
                histories.add(history)
                request(restored, base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex,
                        expected_revision=1, history_id=history, body={'branch': 'restored-' + str(cycle)}))
                request(restored, base + '/mutations', body=dict(mutation, mutation_id=uuid.uuid4().hex,
                        record_id='replacement-after-restore', history_id=history))
                current = request(restored, base + '/snapshot')
                assert current['cursor'] == stale['cursor'] and current['epoch'] == stale['epoch']
                feed = base + '/changes?after=' + str(stale['cursor']) + '&epoch=' + str(stale['epoch'])
                request(restored, feed + '&history_id=' + previous_history, expected=409)
                request(restored, feed, expected=409)  # legacy clients must rebase too
                assert request(restored, feed + '&history_id=' + history)['changes'] == []
                rows = {row['id']: row for row in current['records']}
                assert 'lost-after-backup' not in rows
                assert rows['shared']['body'] == {'branch': 'restored-' + str(cycle)}
                request(restored, base + '/mutations', body=dict(mutation, expected_revision=1,
                        history_id=previous_history), expected=409)
                operator('grant', 'alice', 'recreated-after-restore', '--role', 'admin', pg_env=restored_env)
                recreated = '/v1/workspaces/recreated-after-restore'
                assert request(restored, recreated + '/snapshot')['cursor'] == 0
                request(restored, recreated + '/changes?after=0&epoch=1', expected=409)
                previous_history = history
            print('PASS: repeated restore histories differ; caught-up stale and legacy cursors reset; stale mutations rejected; recreated workspaces require history')
            print('PASS: quiesced metadata+artifact backup, separate PostgreSQL17 restore, byte integrity, private ACL, reader role, revoked token and hash-only persistence')
            print('PASS: checksum and path traversal rejection before restore, nonempty target preserved, writer restarted after backup failure')
        finally:
            for container in containers:
                docker('rm', '-f', container, check=False)
                assert docker('container', 'inspect', container, check=False).returncode != 0
            for volume in volumes:
                docker('volume', 'rm', volume, check=False)
                assert docker('volume', 'inspect', volume, check=False).returncode != 0
            docker('network', 'rm', name, check=False)
            assert docker('network', 'inspect', name, check=False).returncode != 0
            print('CLEANUP VERIFIED: owned backup/restore containers, volumes and network removed')


if __name__ == '__main__':
    main()
