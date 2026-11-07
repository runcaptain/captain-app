"""Offline operator controls, authorized by direct database access, never HTTP."""
import hashlib
import secrets

from sqlalchemy import and_, insert, select, update

from .identity import OIDCSettings
from .migrations import locked_transaction, require_current
from .server import identities, members, users, workspaces


def enroll(engine, *, issuer, subject, user_id, workspace, role):
    OIDCSettings(issuer, 'operator-validation', issuer)
    for value, limit in ((subject, 500), (user_id, 100), (workspace, 100)):
        if not value or len(value) > limit:
            raise ValueError('Invalid identity or workspace identifier')
    if role not in ('reader', 'editor', 'admin'):
        raise ValueError('Invalid role')
    require_current(engine)
    with locked_transaction(engine) as db:
        existing = db.execute(select(identities.c.user_id).where(and_(identities.c.issuer == issuer, identities.c.subject == subject))).scalar()
        if existing and existing != user_id:
            raise ValueError('Identity binding is immutable; refusing reassignment')
        if not db.execute(select(users.c.id).where(users.c.id == user_id)).first():
            # Disabled opaque credential; the random preimage is never returned or stored.
            db.execute(insert(users).values(id=user_id, token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        if not existing:
            db.execute(insert(identities).values(issuer=issuer, subject=subject, user_id=user_id))
        _grant(db, user_id, workspace, role)


def grant(engine, *, user_id, workspace, role):
    for value in (user_id, workspace):
        if not value or len(value) > 100:
            raise ValueError('Invalid user or workspace identifier')
    if role not in ('reader', 'editor', 'admin'):
        raise ValueError('Invalid role')
    require_current(engine)
    with locked_transaction(engine) as db:
        if not db.execute(select(users.c.id).where(users.c.id == user_id)).first():
            db.execute(insert(users).values(id=user_id, token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        _grant(db, user_id, workspace, role)


def _grant(db, user_id, workspace, role):
    w = db.execute(select(workspaces).where(workspaces.c.id == workspace).with_for_update()).mappings().first()
    if not w:
        db.execute(insert(workspaces).values(id=workspace, cursor=0, epoch=1))
    where = and_(members.c.workspace == workspace, members.c.user_id == user_id)
    prior = db.execute(select(members.c.role).where(where)).scalar()
    if prior is None:
        db.execute(insert(members).values(workspace=workspace, user_id=user_id, role=role))
    else:
        db.execute(update(members).where(where).values(role=role))
    if w and prior != role:
        db.execute(update(workspaces).where(workspaces.c.id == workspace).values(epoch=w['epoch'] + 1))


def revoke(engine, *, workspace, user_id):
    require_current(engine)
    with locked_transaction(engine) as db:
        w = db.execute(select(workspaces).where(workspaces.c.id == workspace).with_for_update()).mappings().first()
        where = and_(members.c.workspace == workspace, members.c.user_id == user_id)
        role = db.execute(select(members.c.role).where(where)).scalar()
        if not w or role is None:
            raise ValueError('Membership does not exist')
        if role != 'revoked':
            db.execute(update(members).where(where).values(role='revoked'))
            db.execute(update(workspaces).where(workspaces.c.id == workspace).values(epoch=w['epoch'] + 1))


def list_members(engine, workspace):
    require_current(engine)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(select(members.c.user_id, members.c.role).where(members.c.workspace == workspace).order_by(members.c.user_id)).mappings()]
