"""Operator-issued bearer credentials. Only hashes are persisted in the database."""
import hashlib
import os
from pathlib import Path
import secrets
import time
import uuid

from sqlalchemy import BigInteger, Column, String, Table, and_, insert, select, update

from .migrations import locked_transaction, require_current
from .server import metadata, users

tokens = Table('sync_access_tokens', metadata,
    Column('id', String(32), primary_key=True),
    Column('user_id', String(100), nullable=False, index=True),
    Column('token_hash', String(64), nullable=False, unique=True),
    Column('issued_at', BigInteger, nullable=False),
    Column('expires_at', BigInteger, nullable=False),
    Column('revoked_at', BigInteger))


def authenticate(db, credential):
    if not credential.startswith('cst_') or len(credential) != 68:
        return None
    hashed = hashlib.sha256(credential.encode()).hexdigest()
    return db.execute(select(tokens.c.user_id).where(and_(
        tokens.c.token_hash == hashed, tokens.c.revoked_at.is_(None),
        tokens.c.expires_at > int(time.time())))).scalar()


def issue(engine, *, user_id=None, output, expires_in_days=30, rotate_id=None):
    """Write once to a private file, then atomically activate and optionally rotate.

    A failed file write leaves the old credential valid. A failed database commit
    removes the newly written file. A process crash before commit can leave a file
    containing an unusable token; it never partially commits a rotation.
    """
    if not isinstance(expires_in_days, int) or not 1 <= expires_in_days <= 365:
        raise ValueError('Token lifetime must be between 1 and 365 days')
    if bool(user_id) == bool(rotate_id):
        raise ValueError('Supply either a user or a token to rotate')
    require_current(engine)
    path = Path(output)
    created = False
    credential = 'cst_' + secrets.token_urlsafe(48)
    token_id = uuid.uuid4().hex
    now = int(time.time())
    result = dict(id=token_id, user_id=user_id, issued_at=now,
                  expires_at=now + expires_in_days * 86400, revoked_at=None)
    try:
        with locked_transaction(engine) as db:
            if rotate_id:
                previous = db.execute(select(tokens).where(tokens.c.id == rotate_id)).mappings().first()
                if not previous or previous['revoked_at'] is not None:
                    raise ValueError('Token does not exist or is already revoked')
                user_id = previous['user_id']
                result['user_id'] = user_id
            if not db.execute(select(users.c.id).where(users.c.id == user_id)).first():
                raise ValueError('User does not exist; grant membership first')
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except OSError:
                raise ValueError('Token output must be a new file in an existing private directory') from None
            created = True
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(credential + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            db.execute(insert(tokens).values(**result,
                token_hash=hashlib.sha256(credential.encode()).hexdigest()))
            if rotate_id:
                db.execute(update(tokens).where(tokens.c.id == rotate_id).values(revoked_at=now))
        return result
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def list_tokens(engine, *, user_id=None):
    require_current(engine)
    query = select(tokens.c.id, tokens.c.user_id, tokens.c.issued_at,
                   tokens.c.expires_at, tokens.c.revoked_at).order_by(tokens.c.issued_at, tokens.c.id)
    if user_id:
        query = query.where(tokens.c.user_id == user_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(query).mappings()]


def revoke_token(engine, token_id):
    require_current(engine)
    with locked_transaction(engine) as db:
        row = db.execute(select(tokens.c.revoked_at).where(tokens.c.id == token_id)).first()
        if row is None:
            raise ValueError('Token does not exist')
        if row.revoked_at is None:
            db.execute(update(tokens).where(tokens.c.id == token_id).values(revoked_at=int(time.time())))
