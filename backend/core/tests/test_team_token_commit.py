"""Synthetic commit failures must not destroy the only usable replacement token."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest
from sqlalchemy import create_engine, event

from captain_search.collaboration import tokens
from captain_search.collaboration.migrations import migrate
from captain_search.collaboration.operator import grant


@pytest.fixture
def setup(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'tokens.db'))
    migrate(engine)
    grant(engine, user_id='alice', workspace='team', role='admin')
    old_path = tmp_path / 'old.token'
    old = tokens.issue(engine, user_id='alice', output=old_path)
    yield engine, old, old_path, tmp_path / 'replacement.token'
    engine.dispose()


def authorized(engine, path):
    with engine.connect() as db:
        return tokens.authenticate(db, path.read_text().strip())


def test_lost_commit_reply_retains_committed_replacement(setup, monkeypatch):
    engine, old, old_path, replacement = setup
    original = tokens.locked_transaction
    @contextmanager
    def lost_reply(engine):
        with original(engine) as db:
            yield db
        raise RuntimeError('synthetic lost commit reply')
    monkeypatch.setattr(tokens, 'locked_transaction', lost_reply)
    with pytest.raises(RuntimeError, match='lost commit reply'):
        tokens.issue(engine, rotate_id=old['id'], output=replacement)
    assert replacement.stat().st_mode & 0o777 == 0o600
    assert authorized(engine, old_path) is None
    assert authorized(engine, replacement) == 'alice'


def test_unfinished_commit_cannot_be_mistaken_for_rollback(setup, monkeypatch):
    engine, old, old_path, replacement = setup
    original = tokens.locked_transaction
    confirmation_started = threading.Event()
    release_commit = threading.Event()
    workers = []
    calls = [0]
    @contextmanager
    def interrupted_commit(engine):
        calls[0] += 1
        if calls[0] != 1:
            confirmation_started.set()
            with original(engine) as db:
                yield db
            return
        db = engine.connect()
        db.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            yield db
        except BaseException:
            db.rollback()
            db.close()
            raise
        def finish():
            try:
                if release_commit.wait(5):
                    db.commit()
                else:
                    db.rollback()
            finally:
                db.close()
        worker = threading.Thread(target=finish)
        workers.append(worker)
        worker.start()
        raise RuntimeError('synthetic reply lost before commit finishes')
    monkeypatch.setattr(tokens, 'locked_transaction', interrupted_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tokens.issue, engine, rotate_id=old['id'], output=replacement)
        try:
            assert confirmation_started.wait(3)
            assert not future.done(), 'cleanup must wait for the original transaction lock'
            assert replacement.exists()
        finally:
            release_commit.set()
        with pytest.raises(RuntimeError, match='before commit finishes'):
            future.result(timeout=5)
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert authorized(engine, old_path) is None
    assert authorized(engine, replacement) == 'alice'


def test_unknown_cleanup_outcome_retains_private_file(setup, monkeypatch):
    engine, old, old_path, replacement = setup
    original = tokens.locked_transaction
    calls = [0]
    @contextmanager
    def unavailable_after_commit(engine):
        calls[0] += 1
        if calls[0] > 1:
            raise RuntimeError('synthetic verification connection unavailable')
        with original(engine) as db:
            yield db
        raise RuntimeError('synthetic lost reply')
    monkeypatch.setattr(tokens, 'locked_transaction', unavailable_after_commit)
    with pytest.raises(RuntimeError, match='lost reply'):
        tokens.issue(engine, rotate_id=old['id'], output=replacement)
    assert replacement.stat().st_mode & 0o777 == 0o600
    assert authorized(engine, old_path) is None
    assert authorized(engine, replacement) == 'alice'


def test_confirmed_rollback_removes_new_file_and_preserves_old_token(setup):
    engine, old, old_path, replacement = setup
    def fail_before_commit(connection):
        raise RuntimeError('synthetic confirmed rollback')
    event.listen(engine, 'commit', fail_before_commit)
    try:
        with pytest.raises(RuntimeError, match='confirmed rollback'):
            tokens.issue(engine, rotate_id=old['id'], output=replacement)
    finally:
        event.remove(engine, 'commit', fail_before_commit)
    assert not replacement.exists()
    assert authorized(engine, old_path) == 'alice'
