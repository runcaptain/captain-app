"""Persistent local metadata cache and outbox for the collaboration spike.

No artifact bytes or credentials are stored here. Cache files are per server,
principal and workspace. Pending edits are drafts, never compute submissions.
"""
import json
import sqlite3
import uuid


def write_permission_denied(response):
    if response.status_code != 403:
        return False
    try:
        return response.json().get('detail') in ('write_denied', 'only_owner_changes_visibility', 'admin_required')
    except (ValueError, AttributeError):
        return False


class SyncClient:
    def __init__(self, path, http, workspace, identity):
        self.http, self.workspace = http, workspace
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT);
        ''')
        scope = json.dumps([str(http.base_url), identity, workspace])
        old = self._get('scope')
        if old and old != scope:
            raise ValueError('Use a separate cache for each server, identity and workspace')
        with self.db:
            self._set('scope', scope)

    def _get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, json.dumps(value)))

    def _url(self, operation):
        from urllib.parse import quote
        return f'/v1/workspaces/{quote(self.workspace, safe="")}/{operation}'

    def _check(self, response):
        if response.status_code in (401, 403) and not write_permission_denied(response):
            # Expiry hides cached data and proposals until reauthentication.
            # Revocation additionally erases proposals at the next contact.
            with self.db:
                self.db.execute('DELETE FROM records')
                if response.status_code == 403:
                    self.db.execute('DELETE FROM outbox')
                self._set('permission_reset_pending', True)
                self.db.execute("DELETE FROM meta WHERE key IN ('cursor','epoch')")
        response.raise_for_status()
        return response.json()

    def bootstrap(self):
        data = self._check(self.http.get(self._url('snapshot')))
        with self.db:
            self.db.execute('DELETE FROM records')
            for record in data['records']:
                self._store(record)
            allowed = {record['id'] for record in data['records'] if not record.get('deleted')}
            for mutation_id, payload in self.db.execute('SELECT id,payload FROM outbox').fetchall():
                mutation = json.loads(payload)
                if mutation['expected_revision'] > 0 and mutation['record_id'] not in allowed:
                    self.db.execute('DELETE FROM outbox WHERE id=?', (mutation_id,))
            self._set('cursor', data['cursor'])
            self._set('epoch', data['epoch'])
            self._set('permission_reset_pending', False)
        return data

    def _store(self, record):
        if record.get('deleted'):
            self.db.execute('DELETE FROM records WHERE id=?', (record['id'],))
        else:
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',
                            (record['id'], json.dumps(record)))

    def pull(self):
        if self._get('cursor') is None:
            return self.bootstrap()
        while True:
            response = self.http.get(self._url('changes'), params={
                'after': self._get('cursor'), 'epoch': self._get('epoch'), 'limit': 100})
            if response.status_code == 409:
                # Authorization or visibility changed. Never retain old shared rows.
                with self.db:
                    self.db.execute('DELETE FROM records')
                    self._set('permission_reset_pending', True)
                    self.db.execute("DELETE FROM meta WHERE key IN ('cursor','epoch')")
                return self.bootstrap()
            data = self._check(response)
            with self.db:
                for change in data['changes']:
                    if change['op'] == 'delete':
                        self.db.execute('DELETE FROM records WHERE id=?', (change['record_id'],))
                    else:
                        self._store(change['record'])
                self._set('cursor', data['cursor'])
                self._set('epoch', data['epoch'])
            if not data['has_more']:
                return data

    def queue_edit(self, record_id, kind, body, visibility=None, expected_revision=None):
        record = self.get(record_id)
        if visibility is None:
            visibility = record['visibility'] if record else 'private'
        if expected_revision is None:
            expected_revision = record['revision'] if record else 0
        mutation = {'mutation_id': str(uuid.uuid4()), 'record_id': record_id,
                    'kind': kind, 'expected_revision': expected_revision,
                    'visibility': visibility, 'body': body, 'deleted': False}
        with self.db:
            self.db.execute('INSERT INTO outbox(id,payload) VALUES (?,?)',
                            (mutation['mutation_id'], json.dumps(mutation)))
        return mutation['mutation_id']

    def flush(self):
        # A lost acknowledgement leaves the same durable mutation ID queued.
        # Conflicts require user resolution; never silently rebase scientific configs.
        self.pull()
        rows = self.db.execute("SELECT id,payload FROM outbox WHERE state='pending' ORDER BY rowid").fetchall()
        for mutation_id, payload in rows:
            response = self.http.post(self._url('mutations'), json=json.loads(payload))
            if response.status_code == 404:
                with self.db:
                    self.db.execute('DELETE FROM records WHERE id=?', (json.loads(payload)['record_id'],))
                    self.db.execute('DELETE FROM outbox WHERE id=?', (mutation_id,))
                continue
            if response.status_code == 409:
                with self.db:
                    self.db.execute("UPDATE outbox SET state='conflict',error=? WHERE id=?",
                                    (response.text, mutation_id))
                continue
            self._check(response)
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE id=?', (mutation_id,))
        return self.pull()

    def get(self, record_id):
        row = self.db.execute('SELECT data FROM records WHERE id=?', (record_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def drafts(self):
        if self._get('permission_reset_pending'):
            return []
        return [{'mutation': json.loads(p), 'state': s, 'error': e}
                for p, s, e in self.db.execute('SELECT payload,state,error FROM outbox ORDER BY rowid')]

    def discard(self, mutation_id):
        """Remove a local proposal only; never undo an acknowledged remote write."""
        with self.db:
            removed = self.db.execute('DELETE FROM outbox WHERE id=?', (mutation_id,)).rowcount
        if not removed:
            raise ValueError('Local draft not found')

    def resolve(self, mutation_id, body):
        """Replace a conflict with an explicitly reviewed proposal at the cached revision."""
        if self._get('permission_reset_pending'):
            raise ValueError('Pull current permissions before resolving a conflict')
        row = self.db.execute('SELECT payload,state FROM outbox WHERE id=?', (mutation_id,)).fetchone()
        if not row or row[1] != 'conflict':
            raise ValueError('Only a conflicting draft can be resolved')
        mutation = json.loads(row[0])
        current = self.get(mutation['record_id'])
        if not current:
            raise ValueError('The record is no longer in the authorized cache')
        replacement = str(uuid.uuid4())
        mutation.update(mutation_id=replacement, body=body, expected_revision=current['revision'])
        with self.db:
            self.db.execute('INSERT INTO outbox(id,payload) VALUES (?,?)', (replacement, json.dumps(mutation)))
            self.db.execute('DELETE FROM outbox WHERE id=?', (mutation_id,))
        return replacement

    def close(self):
        self.db.close()
