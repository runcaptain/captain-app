"""Forward-only schema versions. Operator DB access is the migration authority."""
from contextlib import contextmanager
import uuid

from sqlalchemy import Column, Integer, MetaData, Table, insert, inspect, select, text, update

LATEST_VERSION = 4
migration_metadata = MetaData()
versions = Table('sync_schema_versions', migration_metadata,
                 Column('version', Integer, primary_key=True))


@contextmanager
def locked_transaction(engine):
    with engine.connect() as db:
        if engine.dialect.name == 'sqlite':
            db.exec_driver_sql('BEGIN IMMEDIATE')
        elif engine.dialect.name == 'postgresql':
            db.begin()
            db.execute(text('SELECT pg_advisory_xact_lock(742081321)'))
        else:
            raise ValueError('Team server requires PostgreSQL or synthetic SQLite')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def migrate(engine):
    # Import registers artifact tables before creating the initial schema.
    from . import artifacts  # noqa: F401
    from .server import metadata, identities, workspaces
    from .tokens import tokens
    with locked_transaction(engine) as db:
        inspector = inspect(db)
        applied_before = set(db.execute(select(versions.c.version)).scalars()) if inspector.has_table(versions.name) else set()
        for table in metadata.sorted_tables:
            if inspector.has_table(table.name):
                existing = {column["name"] for column in inspector.get_columns(table.name)}
                expected = set(table.c.keys())
                if table is workspaces and 4 not in applied_before:
                    expected -= {'history_id', 'history_required'}
                if table is artifacts.uploads and 4 not in applied_before:
                    expected.discard('history_id')
                primary = set(inspector.get_pk_constraint(table.name)["constrained_columns"])
                if not expected <= existing or primary != {column.name for column in table.primary_key}:
                    raise ValueError("Existing team schema is incompatible; refusing adoption")
        migration_metadata.create_all(db)
        applied = set(db.execute(select(versions.c.version)).scalars())
        if applied != set(range(1, len(applied) + 1)) or applied - set(range(1, LATEST_VERSION + 1)):
            raise ValueError('Unsupported schema version; refusing migration')
        if 1 not in applied:
            metadata.create_all(db, tables=[table for table in metadata.sorted_tables if table is not identities and table is not tokens])
            db.execute(insert(versions).values(version=1))
        if 2 not in applied:
            identities.create(db, checkfirst=True)
            db.execute(insert(versions).values(version=2))
        if 3 not in applied:
            tokens.create(db, checkfirst=True)
            db.execute(insert(versions).values(version=3))
        if 4 not in applied:
            columns = {column['name'] for column in inspect(db).get_columns(workspaces.name)}
            if 'history_id' not in columns:
                db.execute(text("ALTER TABLE sync_workspaces ADD COLUMN history_id VARCHAR(32) NOT NULL DEFAULT ''"))
            if 'history_required' not in columns:
                db.execute(text('ALTER TABLE sync_workspaces ADD COLUMN history_required BOOLEAN NOT NULL DEFAULT FALSE'))
            for workspace in db.execute(select(workspaces.c.id, workspaces.c.history_id)).mappings():
                if not workspace['history_id']:
                    db.execute(update(workspaces).where(workspaces.c.id == workspace['id']).values(history_id=uuid.uuid4().hex))
            upload_columns = {column['name'] for column in inspect(db).get_columns(artifacts.uploads.name)}
            if 'history_id' not in upload_columns:
                db.execute(text('ALTER TABLE sync_artifact_uploads ADD COLUMN history_id VARCHAR(32)'))
            db.execute(text('UPDATE sync_artifact_uploads SET history_id = (SELECT history_id FROM sync_workspaces WHERE sync_workspaces.id = sync_artifact_uploads.workspace) WHERE history_id IS NULL'))
            db.execute(insert(versions).values(version=4))
    return LATEST_VERSION


def require_current(engine):
    from sqlalchemy import inspect
    if not inspect(engine).has_table(versions.name):
        raise ValueError('Run cst team-server migrate before serving')
    with engine.connect() as db:
        applied = set(db.execute(select(versions.c.version)).scalars())
    if applied != set(range(1, LATEST_VERSION + 1)):
        raise ValueError('Unsupported schema version; run the matching migration command')
