"""Authenticated encrypted application backups: typed rows AND referenced objects."""
import base64
import hashlib
import io
import json
import os
import struct
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import DateTime,LargeBinary,Numeric,func,select,update
from botocore.exceptions import ClientError

from app.core.config import settings
from app.models.database import Base,RestoreCheckpoint,utc_now
from app.services.deployment import expected_head
from app.services.deployment.transfer import _schema,_keys,_references,_digest,reset_sequences
from app.services.storage.objects import read_object,validate_ref,backend_for

MAGIC=b'RQBACKUP\x01'
CHUNK=4*1024*1024
MAX_ROW=256*1024*1024


class BackupError(ValueError):
    pass


def _key(salt):
    try:
        key=(settings.qbo_token_encryption_key or '').encode();Fernet(key)
        raw=base64.urlsafe_b64decode(key)
    except (ValueError,TypeError):
        raise BackupError('Keep the original valid QBO_TOKEN_ENCRYPTION_KEY outside the backup') from None
    return HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,info=b'receipt-ai-backup-v1').derive(raw)


def _encrypt(source,destination):
    salt,nonce=os.urandom(16),os.urandom(8)
    header=MAGIC+salt+nonce;cipher=AESGCM(_key(salt));counter=0
    destination.write(header)
    while True:
        content=source.read(CHUNK)
        index=counter.to_bytes(4,'big')
        sealed=cipher.encrypt(nonce+index,content,header+index)
        destination.write(struct.pack('>I',len(sealed)));destination.write(sealed)
        if not content:break  # Authenticated EOF; truncation is never accepted.
        counter+=1


def _decrypt(source,destination):
    header=source.read(len(MAGIC)+24)
    if len(header)!=len(MAGIC)+24 or not header.startswith(MAGIC):raise BackupError('Unknown or truncated backup header')
    salt,nonce=header[len(MAGIC):len(MAGIC)+16],header[-8:];cipher=AESGCM(_key(salt));counter=0
    try:
        while True:
            length=source.read(4)
            if len(length)!=4:raise BackupError('Backup is truncated; authenticated EOF is missing')
            size=struct.unpack('>I',length)[0]
            if not 16<=size<=CHUNK+16:raise BackupError('Invalid backup frame')
            sealed=source.read(size)
            if len(sealed)!=size:raise BackupError('Backup is truncated')
            index=counter.to_bytes(4,'big');content=cipher.decrypt(nonce+index,sealed,header+index)
            if not content:
                if source.read(1):raise BackupError('Unexpected bytes after authenticated EOF')
                return
            destination.write(content);counter+=1
    except InvalidTag:
        raise BackupError('Backup authentication failed; wrong key or changed bytes') from None


def _encode(column,value):
    if value is None:return None
    if isinstance(column.type,LargeBinary):return base64.b64encode(bytes(value)).decode()
    if isinstance(column.type,DateTime):return value.isoformat()
    if isinstance(column.type,Numeric):return str(value)
    return value


def _decode(table,row):
    if not isinstance(row,dict) or set(row)!=set(table.c.keys()):raise BackupError('Backup columns do not match this code version')
    decoded={}
    for column in table.columns:
        value=row[column.name]
        if value is not None:
            if isinstance(column.type,LargeBinary):value=base64.b64decode(value,validate=True)
            elif isinstance(column.type,DateTime):value=datetime.fromisoformat(value)
            elif isinstance(column.type,Numeric):value=Decimal(value)
        decoded[column.name]=value
    return decoded


@contextmanager
def _archive(path):
    with tempfile.TemporaryDirectory(prefix='receipt-ai-restore-') as directory:
        plain=Path(directory)/'backup.zip'
        try:
            with Path(path).open('rb') as source,plain.open('wb') as out:_decrypt(source,out)
            with zipfile.ZipFile(plain) as archive:yield archive
        except (zipfile.BadZipFile,KeyError,TypeError,ValueError) as exc:
            if isinstance(exc,BackupError):raise
            raise BackupError('Invalid backup structure or typed data') from None


def _rows(archive,table):
    with archive.open('tables/'+table.name+'.jsonl') as stream:
        while data:=stream.readline(MAX_ROW+1):
            if len(data)>MAX_ROW:raise BackupError('Backup row exceeds the supported limit')
            yield _decode(table,json.loads(data))


def _verified(archive):
    info=archive.getinfo('manifest.json')
    if info.file_size>10*1024*1024:raise BackupError('Backup manifest is too large')
    manifest=json.loads(archive.read(info))
    if manifest['format']!=1 or manifest['head']!=expected_head() or set(manifest['tables'])!=set(Base.metadata.tables):
        raise BackupError('Backup schema/version does not match this application; use the matching code and migrations')
    uuid.UUID(manifest['backup_id']);datetime.fromisoformat(manifest['snapshot_at'])
    wanted={'manifest.json'}|{'tables/'+name+'.jsonl' for name in manifest['tables']}|{'objects/'+digest for digest in manifest['objects']}
    names=archive.namelist()
    if len(names)!=len(set(names)) or set(names)!=wanted:raise BackupError('Unexpected or duplicate archive members')
    refs=[]
    for table in Base.metadata.sorted_tables:
        metadata=manifest['tables'][table.name];digest=hashlib.sha256();count=0
        with archive.open('tables/'+table.name+'.jsonl') as stream:
            while data:=stream.readline(MAX_ROW+1):
                if len(data)>MAX_ROW:raise BackupError('Backup row exceeds the supported limit')
                row=_decode(table,json.loads(data));digest.update(data);count+=1
                if table.name=='documents':
                    for ref,role in _references(row):
                        validate_ref(ref,row['company_id'],row['api_id'],role)
                        refs.append((ref,row['company_id'],row['api_id'],role))
        if count!=metadata['rows'] or digest.hexdigest()!=metadata['sha256']:raise BackupError('Backup table count/checksum mismatch')
    digests={ref['sha256'] for ref,_,_,_ in refs}
    if digests!=set(manifest['objects']):raise BackupError('Backup object set differs from document references')
    for digest,metadata in manifest['objects'].items():
        if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise BackupError('Invalid object digest')
        size=metadata['size']
        if type(size)is not int or size<=0:raise BackupError('Invalid object size')
        actual=hashlib.sha256();length=0
        with archive.open('objects/'+digest) as source:
            while chunk:=source.read(CHUNK):actual.update(chunk);length+=len(chunk)
        if length!=size or actual.hexdigest()!=digest:raise BackupError('Backup object checksum mismatch')
    for ref,_,_,_ in refs:
        if manifest['objects'][ref['sha256']]['size']!=ref['size']:raise BackupError('Object reference size mismatch')
    return manifest,refs


def _summary(manifest):
    return {'backup_id':manifest['backup_id'],'snapshot_at':manifest['snapshot_at'],'head':manifest['head'],
            'rows':{name:item['rows'] for name,item in manifest['tables'].items()},'objects':len(manifest['objects'])}


def verify(path):
    with _archive(path) as archive:
        manifest,_=_verified(archive)
        return dict(_summary(manifest),verified=True)


def create(engine,path):
    path=Path(path);_key(os.urandom(16))
    if path.exists():raise BackupError('Backup output already exists; choose a new filename')
    with tempfile.TemporaryDirectory(prefix='receipt-ai-backup-') as directory:
        plain=Path(directory)/'backup.zip'
        snapshot_at=datetime.now(timezone.utc).isoformat()
        options={'isolation_level':'REPEATABLE READ'} if engine.dialect.name=='postgresql' else {}
        with engine.connect().execution_options(**options) as conn:
            if conn.dialect.name=='sqlite':conn.exec_driver_sql('BEGIN')
            _schema(conn);_keys(conn)
            if conn.dialect.name=='postgresql':
                conn.exec_driver_sql('LOCK TABLE '+', '.join(conn.dialect.identifier_preparer.quote(t.name) for t in Base.metadata.sorted_tables)+' IN SHARE MODE')
            manifest={'format':1,'head':expected_head(),'backup_id':str(uuid.uuid4()),
                'snapshot_at':snapshot_at,'tables':{},'objects':{}}
            refs=[]
            with zipfile.ZipFile(plain,'w',zipfile.ZIP_DEFLATED) as archive:
                for table in Base.metadata.sorted_tables:
                    digest=hashlib.sha256();count=0
                    with archive.open('tables/'+table.name+'.jsonl','w') as stream:
                        for row in conn.execute(select(table).order_by(*table.primary_key.columns)).mappings():
                            encoded={column.name:_encode(column,row[column.name]) for column in table.columns}
                            data=(json.dumps(encoded,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
                            if len(data)>MAX_ROW:raise BackupError('A database row exceeds the backup format limit')
                            stream.write(data);digest.update(data);count+=1
                            if table.name=='documents':
                                refs.extend((ref,row['company_id'],row['api_id'],role) for ref,role in _references(row))
                    manifest['tables'][table.name]={'rows':count,'sha256':digest.hexdigest(),'database_digest':_digest(conn,table)[1]}
                for ref,company,id,role in refs:
                    validate_ref(ref,company,id,role)
                    if ref['sha256'] in manifest['objects']:continue
                    content=read_object(ref,company,id,role)
                    archive.writestr('objects/'+ref['sha256'],content)
                    manifest['objects'][ref['sha256']]={'size':len(content)}
                archive.writestr('manifest.json',json.dumps(manifest,sort_keys=True))
        # Verify our own plaintext format before ever publishing an encrypted file.
        with zipfile.ZipFile(plain) as archive:_verified(archive)
        path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as out,plain.open('rb') as source:
                _encrypt(source,out);out.flush();os.fsync(out.fileno())
        except BaseException:
            path.unlink(missing_ok=True);raise
    return dict(_summary(manifest),mode='created')


def _put_object(archive,ref,*,cloud_read_only=False):
    backend,_=backend_for(ref['backend'])
    try:
        existing=backend.download(ref['key'],max_bytes=ref['size'])
    except FileNotFoundError:existing=None
    except ClientError as exc:
        if str(exc.response.get('Error',{}).get('Code')) not in {'404','NoSuchKey','NotFound'}:raise
        existing=None
    if existing is not None:
        if len(existing)!=ref['size'] or hashlib.sha256(existing).hexdigest()!=ref['sha256']:
            raise BackupError('Existing target object differs; use clean storage or restore the original object identity')
        return
    if cloud_read_only and ref['backend']!='local':
        raise BackupError('Cloud object is missing; read-only restore drill cannot write to the original bucket')
    with archive.open('objects/'+ref['sha256']) as source:
        backend.upload(source,ref['key'],'application/octet-stream')
    data=backend.download(ref['key'],max_bytes=ref['size'])
    if len(data)!=ref['size'] or hashlib.sha256(data).hexdigest()!=ref['sha256']:raise BackupError('Restored object verification failed')


def restore(engine,path,*,apply=False,cloud_read_only=False):
    with _archive(path) as archive:
        manifest,refs=_verified(archive)
        with engine.begin() as target:
            _schema(target)
            if target.dialect.name=='postgresql':
                target.exec_driver_sql('LOCK TABLE '+', '.join(target.dialect.identifier_preparer.quote(t.name) for t in Base.metadata.sorted_tables)+' IN EXCLUSIVE MODE')
            elif target.dialect.name=='sqlite':target.exec_driver_sql('BEGIN IMMEDIATE')
            if any(target.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables):
                raise BackupError('Restore requires an empty migrated database; existing records are never overwritten')
            if not apply:return dict(_summary(manifest),mode='dry-run')
            for table in Base.metadata.sorted_tables:
                batch=[]
                for row in _rows(archive,table):
                    batch.append(row)
                    if len(batch)==20:target.execute(table.insert(),batch);batch=[]
                if batch:target.execute(table.insert(),batch)
                count,digest=_digest(target,table)
                if count!=manifest['tables'][table.name]['rows'] or digest!=manifest['tables'][table.name]['database_digest']:
                    raise BackupError('Restored row contents differ; target transaction rolled back')
            _keys(target)
            for ref,_,_,_ in refs:_put_object(archive,ref,cloud_read_only=cloud_read_only)
            reset_sequences(target)
            # A database snapshot cannot roll back later QBO writes or emails.
            target.execute(update(RestoreCheckpoint).where(RestoreCheckpoint.active_key=='active').values(active_key=None,released_at=utc_now()))
            checkpoint=str(uuid.uuid4())
            target.execute(RestoreCheckpoint.__table__.insert().values(id=checkpoint,active_key='active',
                backup_id=manifest['backup_id'],snapshot_at=datetime.fromisoformat(manifest['snapshot_at']).replace(tzinfo=None),restored_at=utc_now()))
            return dict(_summary(manifest),mode='restored',restore_id=checkpoint,outbound_hold=True)
