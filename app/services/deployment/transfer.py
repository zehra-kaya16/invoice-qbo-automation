"""Offline typed SQLite -> empty PostgreSQL transfer; never replays workflow."""
import hashlib
import io
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, inspect, select, text

from app.core.config import settings
from app.models.database import Base
from app.services.deployment import expected_head
from app.services.storage.objects import read_object, validate_ref
from app.services.storage.storage import LocalStorage


class TransferError(ValueError):
    pass


def _schema(connection):
    if MigrationContext.configure(connection).get_current_heads() != (expected_head(),):
        raise TransferError('Source and target must both be at the current migration head')
    actual = set(inspect(connection).get_table_names())
    if actual != set(Base.metadata.tables) | {'alembic_version'}:
        raise TransferError('Database tables differ from this application version')
    for table in Base.metadata.sorted_tables:
        if {c['name'] for c in inspect(connection).get_columns(table.name)} != set(table.c.keys()):
            raise TransferError('Database columns differ from this application version')


def _stable(value):
    if isinstance(value, (bytes, memoryview)):
        return {'bytes_sha256':hashlib.sha256(bytes(value)).hexdigest(),'size':len(value)}
    if isinstance(value, (date,datetime)):
        return {'iso':value.isoformat()}
    if isinstance(value,Decimal):
        return {'decimal':str(value)}
    if isinstance(value,dict):
        return {key:_stable(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [_stable(item) for item in value]
    return value


def _digest(connection, table):
    digest=hashlib.sha256();count=0
    for row in connection.execute(select(table).order_by(*table.primary_key.columns)).mappings():
        data=json.dumps(_stable(dict(row)),sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        digest.update(len(data).to_bytes(8,'big'));digest.update(data);count+=1
    return count,digest.hexdigest()


def _idle(connection):
    for name, states in [('batch_extraction_runs',{'running'}),('batch_push_runs',{'running'}),
                         ('notification_outbox',{'sending'}),('webhook_receipts',{'receiving'})]:
        table=Base.metadata.tables[name]
        if connection.scalar(select(func.count()).select_from(table).where(table.c.status.in_(states))):
            raise TransferError('Interrupted operations exist; recover them with all processes stopped before transfer')
    table=Base.metadata.tables['documents']
    for state in connection.scalars(select(table.c.workflow_state)):
        if state and (state.get('extraction_claim') or state.get('qbo_push_state')=='in_progress'
                      or state.get('qbo_attachment_push_status')=='uploading'):
            raise TransferError('A document has an active claim; complete explicit recovery before transfer')
        if state and any(isinstance(item,dict) and item.get('attachment_push_status')=='uploading'
                         for item in state.get('check_match_results') or []):
            raise TransferError('A check attachment has an active claim; recover it before transfer')
        if state and any(isinstance(item,dict) and item.get('qbo_category_push_status') in {'pushing','updating','in_progress'}
                         for item in state.get('category_suggestions') or []):
            raise TransferError('A category has an active claim; recover it before transfer')


def _keys(connection):
    encrypted=[]
    for table,columns in [('qbo_connections',['access_token','refresh_token']),('webhook_integrations',['secret'])]:
        t=Base.metadata.tables[table]
        for row in connection.execute(select(*(t.c[c] for c in columns))):
            encrypted.extend(value for value in row if value)
    if encrypted:
        try:
            cipher=Fernet((settings.qbo_token_encryption_key or '').encode())
            for value in encrypted:cipher.decrypt(value.encode())
        except (InvalidToken,ValueError,TypeError,UnicodeError):
            raise TransferError('The configured encryption key cannot decrypt the source integrations/tokens') from None


def _references(row):
    state=row['workflow_state'] or {}
    if state.get('source_storage'):
        yield state['source_storage'],'original'
    for entry in (state.get('check_image_sources') or {}).values():
        if entry.get('blob_ref'):yield entry['blob_ref'],'check'
    if (state.get('qbo_attachment_source') or {}).get('blob_ref'):
        yield state['qbo_attachment_source']['blob_ref'],'attachment'


def _objects(source, local_from, apply):
    table=Base.metadata.tables['documents'];seen=set();copied=0
    source_storage=None;target_storage=None
    for row in source.execute(select(table.c.api_id,table.c.company_id,table.c.workflow_state)).mappings():
        for ref,role in _references(row):
            try:
                validate_ref(ref,row['company_id'],row['api_id'],role)
                marker=(ref['backend'],ref['location'],ref['key'])
                if marker in seen:continue
                seen.add(marker)
                if ref['backend']=='local':
                    if local_from is None or not Path(local_from).is_dir():
                        raise TransferError('Mount the complete source uploads directory with --local-from')
                    source_storage=source_storage or LocalStorage(str(local_from))
                    content=source_storage.download(ref['key'],max_bytes=ref['size'])
                    if len(content)!=ref['size'] or hashlib.sha256(content).hexdigest()!=ref['sha256']:
                        raise TransferError('A source object is missing or corrupt')
                    if apply:
                        target_storage=target_storage or LocalStorage(settings.local_storage_path)
                        path=target_storage._path(ref['key'])
                        if path.exists() and target_storage.download(ref['key'],max_bytes=ref['size'])!=content:
                            raise TransferError('An existing target storage object differs; restore the correct object tree')
                        if not path.exists():target_storage.upload(io.BytesIO(content),ref['key'],'application/octet-stream')
                        if target_storage.download(ref['key'],max_bytes=ref['size'])!=content:
                            raise TransferError('Copied object verification failed')
                        copied+=1
                else:
                    read_object(ref,row['company_id'],row['api_id'],role)
            except TransferError:raise
            except Exception:
                raise TransferError('Source objects cannot be verified with the current storage configuration') from None
    return len(seen),copied


def transfer(source_engine, target_engine, *, local_from=None, apply=False, require_postgres=True):
    if source_engine.dialect.name!='sqlite' or (require_postgres and target_engine.dialect.name!='postgresql'):
        raise TransferError('Transfer requires a SQLite source and a PostgreSQL target')
    if source_engine.url==target_engine.url:
        raise TransferError('Source and target must differ')
    with source_engine.connect() as source, target_engine.connect() as target:
        source.exec_driver_sql('BEGIN')  # Stable read-only source snapshot.
        _schema(source);_schema(target)
        target.rollback()
        _idle(source);_keys(source)
        with target.begin():
            if target.dialect.name=='postgresql':
                names=', '.join(target.dialect.identifier_preparer.quote(t.name) for t in Base.metadata.sorted_tables)
                target.exec_driver_sql('LOCK TABLE '+names+' IN EXCLUSIVE MODE')
            else:
                target.exec_driver_sql('BEGIN IMMEDIATE')  # Test target only; CLI requires PostgreSQL.
            if any(target.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables):
                raise TransferError('Target is not empty; transfer never merges or overwrites existing records')
            counts={t.name:_digest(source,t)[0] for t in Base.metadata.sorted_tables}
            objects,copied=_objects(source,local_from,apply)
            if not apply:
                return {'mode':'dry-run','rows':counts,'objects_verified':objects,'objects_copied':0}
            for table in Base.metadata.sorted_tables:
                rows=source.execute(select(table)).mappings()
                while batch:=rows.fetchmany(20):
                    target.execute(table.insert(),[dict(row) for row in batch])
                if _digest(source,table)!=_digest(target,table):
                    raise TransferError('Target row contents differ; transaction rolled back')
            if target.dialect.name=='postgresql':
                # Explicit integer IDs do not advance a PostgreSQL sequence.
                table=Base.metadata.tables['auth_login_attempts']
                sequence=target.scalar(text("SELECT pg_get_serial_sequence('auth_login_attempts','id')"))
                if sequence:
                    maximum=target.scalar(select(func.max(table.c.id)))
                    target.execute(text('SELECT setval(CAST(:seq AS regclass), :value, :called)'),
                                   {'seq':sequence,'value':maximum or 1,'called':maximum is not None})
            return {'mode':'applied','rows':counts,'objects_verified':objects,'objects_copied':copied}
