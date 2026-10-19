import base64
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.core.config import settings
from app.main import app
from app.models.database import Base,Document,Company,QBOConnection,WebhookIntegration,NotificationOutbox,RestoreCheckpoint,utc_now
from app.services.deployment import backups,restore_guard
from app.services.persistence.repository import DocumentRepository
from app.services.storage.objects import SourceStorageError
from tests.test_batch_extraction import actor
from tests.test_deployment import stamp

client=TestClient(app)


@pytest.fixture
def bundle(actor,isolated_document_database,monkeypatch,tmp_path):
    key=Fernet.generate_key().decode();monkeypatch.setattr(settings,'qbo_token_encryption_key',key)
    monkeypatch.setattr(settings,'email_enabled',False)
    source=isolated_document_database.kw['bind'];stamp(source)
    crop=b'cropped-check';derivative=b'converted-attachment'
    doc={'id':'doc_backup','filename':'original.pdf','company_id':'default','content_type':'application/pdf','content':b'%PDF-original',
         'status':'pushed','approved':True,'qbo_push_state':'succeeded','qbo_transaction_id':'167',
         'extracted_data':{'invoice_number':'INV-109','currency':'USD','total':19.09},
         'check_image_sources':{'0':{'content_base64':base64.b64encode(crop).decode(),'sha256':hashlib.sha256(crop).hexdigest()}},
         'qbo_attachment_source':{'data_base64':base64.b64encode(derivative).decode(),'sha256':hashlib.sha256(derivative).hexdigest()}}
    DocumentRepository().save(doc)
    cipher=Fernet(key.encode());encrypted=cipher.encrypt(b'not-a-real-provider-token').decode()
    with source.begin() as conn:
        conn.execute(QBOConnection.__table__.insert().values(company_id=actor.company_id,realm_id='backup-test',access_token=encrypted,refresh_token=encrypted))
        conn.execute(WebhookIntegration.__table__.insert().values(company_id=actor.company_id,owner_user_id=actor.user_id,name='Backup',secret=encrypted,enabled=True,auto_extract=False))
        for state in ['accepted','uncertain','pending']:
            conn.execute(NotificationOutbox.__table__.insert().values(company_id=actor.company_id,user_id=actor.user_id,
                recipient_email=actor.email,event_key=state,kind='extraction.succeeded',document_id='doc_backup',filename='original.pdf',status=state))
    path=tmp_path/'snapshot.rqbackup';report=backups.create(source,path)
    target=create_engine('sqlite:///'+str(tmp_path/'restore.db'));Base.metadata.create_all(target);stamp(target)
    original=Path(settings.local_storage_path)
    monkeypatch.setattr(settings,'local_storage_path',str(tmp_path/'restored-objects'))
    try:yield {'source':source,'target':target,'path':path,'report':report,'original':original,'key':key,'actor':actor}
    finally:target.dispose()


def test_encrypted_bundle_covers_sources_crops_tokens_and_states(bundle,monkeypatch):
    result=backups.verify(bundle['path'])
    assert result['verified'] and result['objects']==3 and result['rows']['documents']==1
    raw=bundle['path'].read_bytes()
    assert b'%PDF-original' not in raw and b'cropped-check' not in raw and b'not-a-real-provider-token' not in raw
    assert result['backup_id']==bundle['report']['backup_id']
    assert backups.restore(bundle['target'],bundle['path'])['mode']=='dry-run'
    with bundle['target'].connect() as conn:assert conn.scalar(select(func.count()).select_from(Document))==0
    result=backups.restore(bundle['target'],bundle['path'],apply=True)
    assert result['outbound_hold'] and result['restore_id']
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=bundle['target']))
    doc=DocumentRepository().get('doc_backup')
    assert doc['content']==b'%PDF-original' and doc['qbo_transaction_id']=='167' and doc['approved']
    assert base64.b64decode(doc['check_image_sources']['0']['content_base64'])==b'cropped-check'
    assert base64.b64decode(doc['qbo_attachment_source']['data_base64'])==b'converted-attachment'
    with bundle['target'].connect() as conn:
        assert set(conn.scalars(select(NotificationOutbox.status)))=={'pending','accepted','uncertain'}
        assert Fernet(bundle['key'].encode()).decrypt(conn.scalar(select(QBOConnection.access_token)))==b'not-a-real-provider-token'


@pytest.mark.parametrize('mode',['wrong-key','changed','truncated','no-eof','extra','header'])
def test_changed_or_incomplete_backup_rejected_before_writes(bundle,monkeypatch,mode):
    path=bundle['path'];raw=path.read_bytes()
    if mode=='wrong-key':monkeypatch.setattr(settings,'qbo_token_encryption_key',Fernet.generate_key().decode())
    elif mode=='changed':raw=raw[:70]+bytes([raw[70]^1])+raw[71:]
    elif mode=='truncated':raw=raw[:len(raw)//2]
    elif mode=='no-eof':raw=raw[:-20]
    elif mode=='extra':raw+=b'extra'
    else:raw=b'bad-header'+raw[10:]
    if mode!='wrong-key':path.write_bytes(raw)
    with pytest.raises(backups.BackupError):backups.restore(bundle['target'],path,apply=True)
    with bundle['target'].connect() as conn:assert conn.scalar(select(func.count()).select_from(Document))==0


def rewrite(path,operation):
    plain=io.BytesIO()
    with path.open('rb') as source:backups._decrypt(source,plain)
    with zipfile.ZipFile(io.BytesIO(plain.getvalue())) as archive:data={name:archive.read(name) for name in archive.namelist()}
    operation(data)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,body in data.items():archive.writestr(name,body)
    sealed=io.BytesIO();backups._encrypt(io.BytesIO(output.getvalue()),sealed);path.write_bytes(sealed.getvalue())


@pytest.mark.parametrize('kind',['path','missing','object','table','schema','columns','digest'])
def test_authenticated_but_invalid_archive_cannot_restore(bundle,kind):
    def change(data):
        manifest=json.loads(data['manifest.json'])
        if kind=='path':data['../../bad']=b'no extraction'
        elif kind=='missing':del data['tables/documents.jsonl']
        elif kind=='object':data[next(name for name in data if name.startswith('objects/'))]=b'corrupt'
        elif kind=='table':data['tables/documents.jsonl']+=b'{}\n'
        elif kind=='schema':manifest['head']='old'
        elif kind=='columns':
            row=json.loads(data['tables/documents.jsonl']);row['unexpected']=1
            data['tables/documents.jsonl']=(json.dumps(row)+'\n').encode()
            manifest['tables']['documents']['sha256']=hashlib.sha256(data['tables/documents.jsonl']).hexdigest()
        else:manifest['tables']['documents']['database_digest']='0'*64
        data['manifest.json']=json.dumps(manifest).encode()
    rewrite(bundle['path'],change)
    with pytest.raises(backups.BackupError):backups.restore(bundle['target'],bundle['path'],apply=True)
    with bundle['target'].connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Document))==0
        assert conn.scalar(select(func.count()).select_from(Company))==0


def test_nonempty_database_and_existing_output_never_overwritten(bundle,monkeypatch):
    with bundle['target'].begin() as conn:conn.execute(Company.__table__.insert().values(name='Keep me'))
    with pytest.raises(backups.BackupError,match='empty'):backups.restore(bundle['target'],bundle['path'],apply=True)
    with bundle['target'].connect() as conn:assert conn.scalar(select(Company.name))=='Keep me'
    old=bundle['path'].read_bytes()
    with pytest.raises(backups.BackupError):backups.create(bundle['source'],bundle['path'])
    assert bundle['path'].read_bytes()==old


def test_missing_source_object_aborts_backup(bundle,monkeypatch,tmp_path):
    monkeypatch.setattr(settings,'local_storage_path',str(bundle['original']))
    with bundle['source'].connect() as conn:state=conn.scalar(select(Document.workflow_state))
    (bundle['original']/state['source_storage']['key']).unlink()
    path=tmp_path/'missing.rqbackup'
    with pytest.raises(SourceStorageError):backups.create(bundle['source'],path)
    assert not path.exists()


def test_storage_failure_rolls_back_restored_rows(bundle,monkeypatch):
    def failed(*a,**k):raise backups.BackupError('Storage failed')
    monkeypatch.setattr(backups,'_put_object',failed)
    with pytest.raises(backups.BackupError):backups.restore(bundle['target'],bundle['path'],apply=True)
    with bundle['target'].connect() as conn:assert conn.scalar(select(func.count()).select_from(Company))==0


def test_conflicting_object_is_not_overwritten(bundle):
    with bundle['source'].connect() as conn:ref=conn.scalar(select(Document.workflow_state))['source_storage']
    path=Path(settings.local_storage_path)/ref['key'];path.parent.mkdir(parents=True);path.write_bytes(b'keep-this')
    with pytest.raises(backups.BackupError,match='differs'):backups.restore(bundle['target'],bundle['path'],apply=True)
    assert path.read_bytes()==b'keep-this'


def test_restore_hold_survives_sessions_and_blocks_api_and_workers(bundle,monkeypatch):
    result=backups.restore(bundle['target'],bundle['path'],apply=True)
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=bundle['target']))
    assert restore_guard.status()['active']
    assert client.get('/api/documents/doc_backup/source').status_code==200
    assert client.get('/api/auth/recovery-status').json()['active']
    for path in ['/api/documents/doc_backup/extract','/api/documents/doc_backup/push-to-qbo','/api/batch/upload','/api/webhooks/example/generic','/api/qbo/connect']:
        assert client.post(path).status_code==409,path
    assert client.get('/api/qbo/callback?code=x&state=x').status_code==409
    assert client.get('/api/qbo/status').status_code==409
    assert client.get('/api/export/qbo-import').status_code==409
    monkeypatch.setattr(settings,'email_enabled',True)
    from app.services.integrations import notifications
    from app.services.persistence import batch_extraction
    monkeypatch.setattr(notifications,'send',lambda *a:pytest.fail('Email sent during restore hold'))
    monkeypatch.setattr(batch_extraction,'_claim',lambda *a:pytest.fail('Extraction claimed during restore hold'))
    assert notifications.run_once() is None and batch_extraction.run_once() is None
    with pytest.raises(ValueError):restore_guard.release(str(uuid.uuid4()))
    assert restore_guard.status()['active']
    released=restore_guard.release(result['restore_id']);assert not restore_guard.status()['active']
    assert released['cancelled_pending_notifications']==1
    with bundle['target'].connect() as conn:assert set(conn.scalars(select(NotificationOutbox.status)))=={'accepted','uncertain','cancelled'}


def test_legacy_bytes_and_typed_rows_roundtrip(bundle,monkeypatch,tmp_path):
    from datetime import datetime
    from decimal import Decimal
    from app.models.database import ExtractedData
    with bundle['source'].begin() as conn:
        id=conn.scalar(select(Document.id))
        conn.execute(RestoreCheckpoint.__table__.insert().values(active_key='active',backup_id=str(uuid.uuid4()),snapshot_at=datetime(2026,10,18)))
        conn.execute(ExtractedData.__table__.insert().values(document_id=id,total_amount=Decimal('19.09'),date=datetime(2026,10,18)))
        conn.execute(Document.__table__.insert().values(company_id=bundle['actor'].company_id,api_id='doc_legacy',filename='legacy.pdf',source_content=b'legacy-private',status='uploaded',workflow_state={'id':'doc_legacy','company_id':'default'}))
    monkeypatch.setattr(settings,'local_storage_path',str(bundle['original']))
    path=tmp_path/'typed.rqbackup';backups.create(bundle['source'],path)
    monkeypatch.setattr(settings,'local_storage_path',str(tmp_path/'typed-target'))
    backups.restore(bundle['target'],path,apply=True)
    with bundle['target'].connect() as conn:
        assert conn.scalar(select(ExtractedData.total_amount))==Decimal('19.09')
        assert conn.scalar(select(Document.source_content).where(Document.api_id=='doc_legacy'))==b'legacy-private'


def test_small_encryption_frames_authenticate_final_frame(monkeypatch):
    monkeypatch.setattr(settings,'qbo_token_encryption_key',Fernet.generate_key().decode())
    monkeypatch.setattr(backups,'CHUNK',8)
    encrypted=io.BytesIO();backups._encrypt(io.BytesIO(b'123456789'*10),encrypted)
    decoded=io.BytesIO();backups._decrypt(io.BytesIO(encrypted.getvalue()),decoded)
    assert decoded.getvalue()==b'123456789'*10
    with pytest.raises(backups.BackupError):backups._decrypt(io.BytesIO(encrypted.getvalue()[:-20]),io.BytesIO())


def test_release_cli_requires_both_operator_acknowledgements(monkeypatch):
    import manage_backups
    for args in [['release','--restore-id','x'],['release','--restore-id','x','--all-processes-stopped']]:
        monkeypatch.setattr('sys.argv',['manage_backups.py']+args)
        with pytest.raises(SystemExit) as exc:manage_backups.main()
        assert exc.value.code==2


def test_restore_hold_is_visible_to_a_new_process(bundle):
    import os,subprocess,sys
    result=backups.restore(bundle['target'],bundle['path'],apply=True)
    env=os.environ.copy();env['DATABASE_URL']=str(bundle['target'].url)
    child=subprocess.run([sys.executable,'manage_backups.py','status'],cwd=Path(__file__).resolve().parents[1],
        env=env,capture_output=True,text=True,check=True)
    status=json.loads(child.stdout)
    assert status['active'] and status['restore_id']==result['restore_id']


@pytest.mark.parametrize('kind',['s3','r2'])
@pytest.mark.parametrize('read_only',[True,False])
def test_cloud_restore_drill_cannot_write_missing_objects(monkeypatch,kind,read_only):
    from types import SimpleNamespace
    from botocore.exceptions import ClientError
    data=b'cloud-backup-object';digest=hashlib.sha256(data).hexdigest();saved={}
    def download(key,**kwargs):
        if key not in saved:raise ClientError({'Error':{'Code':'NoSuchKey'}},'GetObject')
        return saved[key]
    def upload(stream,key,content_type):saved[key]=stream.read()
    monkeypatch.setattr(backups,'backend_for',lambda kind:(SimpleNamespace(download=download,upload=upload),'location'))
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:z.writestr('objects/'+digest,data)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as z:
        ref={'backend':kind,'key':'test-object','size':len(data),'sha256':digest}
        if read_only:
            with pytest.raises(backups.BackupError):backups._put_object(z,ref,cloud_read_only=True)
            assert not saved
        else:
            backups._put_object(z,ref);assert saved['test-object']==data
