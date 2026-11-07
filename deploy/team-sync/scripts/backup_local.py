#!/usr/bin/env python3
"""Quiesced Docker PostgreSQL + local-artifact backup. Never prints backup data."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tarfile
import uuid


def docker(*args, output=None):
    result = subprocess.run(['docker', *args], stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError('Docker operation failed; inspect the named service privately')
    return result.stdout.decode().strip() if output is None else None


def sql(args, query):
    return docker('exec', args.postgres, 'psql', '-X', '-U', args.user, '-d', args.database,
                  '-v', 'ON_ERROR_STOP=1', '-Atc', query)


def idle(args):
    if sql(args, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()") != '0':
        raise RuntimeError('Database has active clients; stop every writer and reader before proceeding')


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def helper(args, archive, *command, output=None):
    name = 'cst-backup-tool-' + uuid.uuid4().hex[:12]
    try:
        return docker('run', '--rm', '--name', name, '--network', 'none', '--user', '0',
                      '-v', args.artifacts + ':/artifacts' + (':ro' if args.action == 'backup' else ''),
                      '-v', str(archive.resolve()) + ':/backup:ro', 'postgres:17', *command, output=output)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        remaining = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True)
        if remaining.returncode == 0:
            raise RuntimeError('Owned backup helper container cleanup failed')


def backup(args):
    target = Path(args.output).resolve()
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    running = []
    try:
        # Stop only explicitly listed writers that are currently running. Drain
        # in-flight HTTP requests before taking either side of the backup.
        for container in args.writer:
            environment = dict(item.split('=', 1) for item in json.loads(docker('inspect', '-f', '{{json .Config.Env}}', container)) if '=' in item)
            if environment.get('CST_TEAM_ARTIFACT_BUCKET'):
                raise RuntimeError('S3 deployment requires a separate artifact backup workflow')
            mounts = json.loads(docker('inspect', '-f', '{{json .Mounts}}', container))
            artifact_root = environment.get('CST_TEAM_ARTIFACT_ROOT', '/artifacts')
            if not any(m.get('Type') == 'volume' and m.get('Name') == args.artifacts and m.get('Destination') == artifact_root for m in mounts):
                raise RuntimeError('Listed writer does not use the selected local artifact volume')
            if docker('inspect', '-f', '{{.State.Running}}', container) == 'true':
                running.append(container)
                docker('stop', '--time', '30', container)
        idle(args)
        with (target / 'metadata.dump').open('xb') as out:
            os.chmod(out.name, 0o600)
            docker('exec', args.postgres, 'pg_dump', '-U', args.user, '-d', args.database,
                   '--format=custom', '--no-owner', '--no-acl', output=out)
            out.flush()
            os.fsync(out.fileno())
        with (target / 'artifacts.tar').open('xb') as out:
            os.chmod(out.name, 0o600)
            helper(args, target, 'tar', '--hard-dereference', '-cpf', '-', '-C', '/artifacts', '.', output=out)
            out.flush()
            os.fsync(out.fileno())
        idle(args)
        manifest = {'format': 1, 'storage': 'local', 'database': args.database,
                    'postgres_major': 17,
                    'files': {name: digest(target / name) for name in ('metadata.dump', 'artifacts.tar')}}
        with (target / 'manifest.json').open('x') as out:
            os.chmod(out.name, 0o600)
            json.dump(manifest, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        if os.name != 'nt':
            directory = os.open(target, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        print('BACKUP VERIFIED: metadata and local artifacts captured while writers stopped')
    finally:
        errors = []
        for container in running:
            try:
                docker('start', container)
            except RuntimeError:
                errors.append(container)
        if errors:
            raise RuntimeError('Backup finished or failed, but a listed writer could not restart; inspect it privately')


def validate_archive(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('storage') != 'local' or manifest.get('postgres_major') != 17:
        raise ValueError('Unsupported backup manifest')
    if set(manifest.get('files', {})) != {'metadata.dump', 'artifacts.tar'}:
        raise ValueError('Incomplete backup manifest')
    for name, expected in manifest['files'].items():
        if digest(root / name) != expected:
            raise ValueError('Backup checksum mismatch')
    with tarfile.open(root / 'artifacts.tar', 'r') as archive:
        for entry in archive:
            parts = Path(entry.name).parts
            if Path(entry.name).is_absolute() or '..' in parts or not (entry.isdir() or entry.isfile()):
                raise ValueError('Unsafe artifact archive entry')
            if entry.mode & 0o6000:
                raise ValueError('Unsafe privileged artifact archive mode')


def restore(args):
    root = Path(args.input).resolve()
    validate_archive(root)
    idle(args)
    # Target must be a dedicated database. Never clear unknown user schemas.
    if sql(args, "SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ('public','information_schema') AND nspname !~ '^pg_'") != '0':
        raise RuntimeError('Target contains an unexpected user schema')
    populated = sql(args, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'") != '0'
    artifact_files = helper(args, root, 'find', '/artifacts', '-mindepth', '1', '-print', '-quit')
    if (populated or artifact_files) and not args.replace_existing:
        raise RuntimeError('Restore target is not empty; use a fresh database and volume or explicit --replace-existing')
    if args.replace_existing:
        sql(args, 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;')
        helper(args, root, 'find', '/artifacts', '-mindepth', '1', '-delete')
    # Do not start any API on partial restore. An error leaves the target offline.
    with (root / 'metadata.dump').open('rb') as source:
        result = subprocess.run(['docker', 'exec', '-i', args.postgres, 'pg_restore',
                                 '-U', args.user, '-d', args.database, '--exit-on-error',
                                 '--single-transaction', '--no-owner', '--no-acl'],
                                stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('Database restore failed; target remains offline')
    helper(args, root, 'tar', '-xpf', '/backup/artifacts.tar', '-C', '/artifacts')
    print('RESTORE COMPLETE: metadata and local artifacts restored; verify before starting writers')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for command in ('backup', 'restore'):
        part = sub.add_parser(command)
        part.add_argument('--postgres', required=True, help='Dedicated running PostgreSQL 17 container')
        part.add_argument('--user', required=True)
        part.add_argument('--database', required=True)
        part.add_argument('--artifacts', required=True, help='Dedicated local artifact Docker volume')
        if command == 'backup':
            part.add_argument('--writer', action='append', required=True, help='API container; repeat for every writer')
            part.add_argument('--output', required=True, help='New private backup directory')
        else:
            part.add_argument('--input', required=True)
            part.add_argument('--replace-existing', action='store_true', help='Destructively replace the dedicated target database public schema and artifact volume')
    args = parser.parse_args()
    try:
        for name in (args.user, args.database):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', name):
                raise ValueError('Invalid PostgreSQL identifier; connection strings and flags are forbidden')
        for name in [args.postgres, args.artifacts, *getattr(args, 'writer', [])]:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
                raise ValueError('Invalid Docker resource name')
        docker('volume', 'inspect', args.artifacts)
        version = int(sql(args, 'SHOW server_version_num'))
        if not 170000 <= version < 180000:
            raise ValueError('This backup format requires PostgreSQL 17')
        (backup if args.action == 'backup' else restore)(args)
    except Exception as exc:
        # Database and storage failures can include private data. Print only our
        # constant operational errors, never child stderr or serialized queries.
        if type(exc) in (RuntimeError, ValueError):
            parser.exit(1, str(exc) + '\n')
        parser.exit(1, 'Backup or restore failed; inspect private local files and service state\n')


if __name__ == '__main__':
    main()
