import base64
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select

from app.core.config import settings, Settings
from app.core.auth import current_principal
from app.models.database import Document
from app.services.persistence.repository import DocumentRepository, DocumentConflictError
from app.services.storage.storage import LocalStorage, S3Storage, R2Storage, DocumentStorage
from app.services.storage.objects import SourceStorageError, read_object
from tests.test_document_api_persistence import client, restart_request
from tests.test_batch_extraction import actor


def upload(content=b'%PDF-source', name='invoice.pdf'):
    response=client.post('/api/documents/upload?auto_process=false',files={'file':(name,content,'application/pdf')})
    assert response.status_code==200,response.text
    return response.json()['id']


def raw(id, factory):
    with factory() as s:
        row=s.scalar(select(Document).where(Document.api_id==id))
        return copy.deepcopy(row.workflow_state),row.source_content,row.revision


def object_path(ref):return Path(settings.local_storage_path)/ref['key']


def test_new_original_is_private_object_and_db_only_contains_pointer(isolated_document_database):
    id=upload();state,source,_=raw(id,isolated_document_database)
    assert source is None and state['source_storage']['backend']=='local'
    assert object_path(state['source_storage']).read_bytes()==b'%PDF-source'
    assert 'source_storage' not in client.get('/api/documents/'+id).json()
    assert client.get('/api/documents/'+id+'/source').content==b'%PDF-source'
    assert client.get('/uploads/'+state['storage_key']).status_code==404
    assert client.get('/api/documents').json()['items'][0]['id']==id


def test_original_survives_fresh_process(isolated_document_database):
    import os, subprocess, sys
    id=upload();env=os.environ.copy();env['DATABASE_URL']=str(isolated_document_database.kw['bind'].url)
    program=f"from app.services.persistence.repository import DocumentRepository;import hashlib;print(hashlib.sha256(DocumentRepository().get({id!r})['content']).hexdigest())"
    result=subprocess.run([sys.executable,'-c',program],env=env,capture_output=True,text=True,check=True)
    assert result.stdout.strip()==hashlib.sha256(b'%PDF-source').hexdigest()


@pytest.mark.parametrize('damage',['missing','bytes','location','tenant'])
def test_integrity_or_location_error_fails_closed_without_deleting_metadata(damage,isolated_document_database):
    id=upload();state,_,_=raw(id,isolated_document_database);ref=state['source_storage'];path=object_path(ref)
    if damage=='missing':path.unlink()
    elif damage=='bytes':path.write_bytes(b'corrupt')
    elif damage=='location':settings.local_storage_path=str(path.parent/'different-root')
    else:
        with isolated_document_database() as s:
            row=s.scalar(select(Document).where(Document.api_id==id));state['source_storage']['key']=ref['key'].replace('/documents/','/documents/other/')
            row.workflow_state=state;s.commit()
    response=client.get('/api/documents/'+id+'/source')
    assert response.status_code==503,response.text
    assert client.get('/api/documents').json()['total']==1
    assert client.get('/api/documents/'+id).status_code==200
    assert raw(id,isolated_document_database)[0]['status']=='uploaded'


def test_unauthorized_company_does_not_touch_object_store(monkeypatch):
    id=upload()
    from dataclasses import replace
    from tests.auth_helpers import TEST_PRINCIPAL
    token=current_principal.set(replace(TEST_PRINCIPAL,company_id='b0000000-0000-0000-0000-000000000111',company_key='other'))
    monkeypatch.setattr('app.services.storage.objects.read_object',lambda *a: (_ for _ in ()).throw(AssertionError('must not read')))
    try:assert DocumentRepository().get(id) is None
    finally:current_principal.reset(token)


@pytest.mark.parametrize('key',['../escape','/absolute','a/../../escape','C:/escape','a\\escape','a/./b'])
def test_local_storage_rejects_unsafe_keys(tmp_path,key):
    local=LocalStorage(str(tmp_path/'objects'))
    for op in [lambda:local.upload(io.BytesIO(b'x'),key,'text/plain'),lambda:local.download(key),lambda:local.delete(key)]:
        with pytest.raises(ValueError):op()


def test_local_symlink_cannot_escape(tmp_path):
    local=LocalStorage(str(tmp_path/'objects'));outside=tmp_path/'outside';outside.mkdir()
    try:
        (tmp_path/'objects'/'link').symlink_to(outside,target_is_directory=True)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows does not grant symbolic-link creation permission')
        raise
    with pytest.raises(ValueError):local.upload(io.BytesIO(b'x'),'link/file','text/plain')
    assert not (outside/'file').exists()


def test_local_interrupted_write_keeps_existing_object_and_cleans_temp(tmp_path):
    local=LocalStorage(str(tmp_path));local.upload(io.BytesIO(b'original'),'safe/file','text/plain')
    class Bad:
        def read(self,n):raise OSError('interrupted')
    with pytest.raises(OSError):local.upload(Bad(),'safe/file','text/plain')
    assert local.download('safe/file')==b'original'
    assert [p.name for p in (tmp_path/'safe').iterdir()]==['file']


def test_failed_object_write_never_creates_document(monkeypatch,isolated_document_database):
    monkeypatch.setattr(LocalStorage,'upload',lambda *a,**kw: (_ for _ in ()).throw(OSError('private secret not returned')))
    r=client.post('/api/documents/upload?auto_process=false',files={'file':('invoice.pdf',b'new','application/pdf')})
    assert r.status_code==503 and 'private secret' not in r.text
    assert client.get('/api/documents').json()['total']==0


def test_failed_readback_never_commits_pointer(monkeypatch):
    monkeypatch.setattr(LocalStorage,'download',lambda *a,**kw:b'wrong')
    r=client.post('/api/documents/upload?auto_process=false',files={'file':('i.pdf',b'new','application/pdf')})
    assert r.status_code==503 and client.get('/api/documents').json()['total']==0


def test_stale_save_does_not_upload_or_change_original(monkeypatch):
    id=upload();repo=DocumentRepository();stale=repo.get(id);fresh=repo.get(id);fresh['approved']=True;repo.save(fresh)
    monkeypatch.setattr(LocalStorage,'upload',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('no upload for stale snapshot')))
    with pytest.raises(DocumentConflictError):repo.save(stale)
    assert repo.get(id)['approved']


def test_workflow_updates_do_not_reupload_unchanged_objects(monkeypatch):
    id=upload();doc=DocumentRepository().get(id)
    monkeypatch.setattr(LocalStorage,'upload',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('unchanged object')))
    doc['approved']=True;DocumentRepository().save(doc)
    assert DocumentRepository().get(id)['content']==b'%PDF-source'


def test_check_and_heic_copy_are_stored_as_verified_objects(isolated_document_database):
    id=upload();doc=DocumentRepository().get(id);data=b'\x89PNG\r\n\x1a\ncopy';digest=hashlib.sha256(data).hexdigest();encoded=base64.b64encode(data).decode()
    doc['check_image_sources']={'check-key':{'filename':'check.png','sha256':digest,'content_base64':encoded}}
    doc['qbo_attachment_source']={'filename':'copy.png','content_type':'image/png','sha256':digest,
        'source_sha256':hashlib.sha256(doc['content']).hexdigest(),'data_base64':encoded}
    DocumentRepository().save(doc);state,source,_=raw(id,isolated_document_database)
    assert 'content_base64' not in state['check_image_sources']['check-key']
    assert 'data_base64' not in state['qbo_attachment_source']
    assert source is None
    fresh=DocumentRepository().get(id)
    assert fresh['check_image_sources']['check-key']['content_base64']==encoded
    assert fresh['qbo_attachment_source']['data_base64']==encoded


def test_legacy_db_original_audit_then_migration_preserves_state(isolated_document_database):
    from manage_document_storage import migrate
    id=upload();state,_,_=raw(id,isolated_document_database)
    state.pop('source_storage');state.pop('storage_key')
    state.update(qbo_push_state='succeeded',qbo_transaction_id='146',status='pushed',approved=True)
    with isolated_document_database() as s:
        row=s.scalar(select(Document).where(Document.api_id==id));row.workflow_state=state;row.source_content=b'legacy original';s.commit()
    before=raw(id,isolated_document_database)
    audit=migrate(document_id=id);assert audit['needs_migration']==1 and audit['migrated']==0 and audit['failed']==0
    assert raw(id,isolated_document_database)==before
    result=migrate(True,id);assert result['migrated']==1 and result['failed']==0
    saved,source,_=raw(id,isolated_document_database);assert source is None
    assert saved['status']=='pushed' and saved['qbo_transaction_id']=='146' and saved['approved']
    assert DocumentRepository().get(id)['content']==b'legacy original'
    assert migrate(True,id)['migrated']==0


def test_failed_migration_keeps_legacy_blob(monkeypatch,isolated_document_database):
    from manage_document_storage import migrate
    id=upload();state,_,_=raw(id,isolated_document_database);state.pop('source_storage');state.pop('storage_key')
    with isolated_document_database() as s:
        row=s.scalar(select(Document).where(Document.api_id==id));row.workflow_state=state;row.source_content=b'legacy';s.commit()
    before=raw(id,isolated_document_database)
    monkeypatch.setattr(LocalStorage,'upload',lambda *a,**kw: (_ for _ in ()).throw(OSError('offline')))
    assert migrate(True,id)['failed']==1
    assert raw(id,isolated_document_database)==before


class FakeS3:
    def __init__(self):self.objects={};self.uploads=[];self.bodies=[]
    def upload_fileobj(self,file,bucket,key,ExtraArgs):
        assert 'ACL' not in ExtraArgs
        self.objects[bucket,key]=file.read();self.uploads.append((bucket,key,ExtraArgs))
    def get_object(self,Bucket,Key):
        body=io.BytesIO(self.objects[Bucket,Key]);self.bodies.append(body);return {'Body':body}


@pytest.mark.parametrize('kind',['s3','r2'])
def test_real_adapters_use_private_s3_protocol_and_restore_after_backend_switch(kind,monkeypatch,isolated_document_database):
    fake=FakeS3();options=[]
    monkeypatch.setattr('boto3.client',lambda service,**kw: options.append((service,kw)) or fake)
    monkeypatch.setattr(settings,'storage_type',kind)
    monkeypatch.setattr(settings,'s3_bucket','private-s3');monkeypatch.setattr(settings,'s3_access_key','test');monkeypatch.setattr(settings,'s3_secret_key','secret')
    monkeypatch.setattr(settings,'r2_bucket','private-r2');monkeypatch.setattr(settings,'r2_account_id','account-id');monkeypatch.setattr(settings,'r2_access_key','test');monkeypatch.setattr(settings,'r2_secret_key','secret')
    id=upload();state,source,_=raw(id,isolated_document_database);assert source is None and state['source_storage']['backend']==kind
    assert fake.uploads[0][2]=={'ContentType':'application/pdf'}
    if kind=='r2':assert options[0][1]['endpoint_url']=='https://account-id.r2.cloudflarestorage.com' and options[0][1]['region_name']=='auto'
    monkeypatch.setattr(settings,'storage_type','local')
    assert DocumentRepository().get(id)['content']==b'%PDF-source'
    assert all(b.closed for b in fake.bodies)
    from manage_document_storage import migrate
    assert migrate(True,id)['migrated']==1
    assert raw(id,isolated_document_database)[0]['source_storage']['backend']=='local'
    assert fake.uploads and len(fake.uploads)==1


def test_pydantic_reads_storage_config_from_env_file(tmp_path,monkeypatch):
    monkeypatch.delenv('STORAGE_TYPE');monkeypatch.delenv('LOCAL_STORAGE_PATH')
    config=tmp_path/'settings.env';config.write_text('STORAGE_TYPE=r2\nR2_BUCKET=private\nR2_ACCOUNT_ID=account\nR2_ACCESS_KEY=key\nR2_SECRET_KEY=private-value-111\n')
    value=Settings(_env_file=config);assert value.storage_type=='r2' and value.r2_bucket=='private' and value.r2_secret_key=='private-value-111'
    assert 'private-value-111' not in repr(value)


def test_batch_upload_and_retry_use_same_objects(actor,isolated_document_database):
    from tests.test_batch_upload import upload as batch_upload
    key='ba000000-0000-0000-0000-000000000111'
    first=batch_upload(key).json();files=list(Path(settings.local_storage_path).rglob('*'));originals=[p for p in files if p.is_file()]
    second=batch_upload(key).json();assert first==second
    assert len(originals)==2 and len([p for p in Path(settings.local_storage_path).rglob('*') if p.is_file()])==2
    for id in first['uploaded_documents']:assert raw(id,isolated_document_database)[1] is None


def test_missing_source_blocks_invoice_push_before_qbo(monkeypatch):
    from tests.test_document_api_persistence import seed_invoice,FakeQBO,push_invoice
    doc=seed_invoice();fake=FakeQBO(doc['id']);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    object_path(DocumentRepository().get(doc['id'])['source_storage']).unlink()
    assert push_invoice(doc['id']).status_code==503
    assert fake.writes==fake.attachments==0


def test_batch_partial_storage_failure_rolls_back_every_document(actor,monkeypatch):
    from tests.test_batch_upload import upload as batch_upload
    original=LocalStorage.upload;calls=[]
    def upload_some(self,*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise OSError('second upload failed')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(LocalStorage,'upload',upload_some)
    result=batch_upload('ba000000-0000-0000-0000-000000000112')
    assert result.status_code==503
    assert client.get('/api/documents').json()['total']==0
    assert client.get('/api/batch').json()['total']==0


def test_local_tree_can_move_without_rewriting_persisted_references(tmp_path,monkeypatch):
    import shutil
    id=upload();before=DocumentRepository().get(id)
    moved=tmp_path/'restored-volume';shutil.copytree(settings.local_storage_path,moved)
    monkeypatch.setattr(settings,'local_storage_path',str(moved))
    assert DocumentRepository().get(id)==before


def test_changed_local_store_identity_is_not_silently_accepted(monkeypatch):
    id=upload();monkeypatch.setattr(settings,'local_storage_id','another-local-store')
    assert client.get('/api/documents/'+id+'/source').status_code==503


def test_cli_apply_requires_stopped_process_flag(monkeypatch):
    import manage_document_storage
    monkeypatch.setattr('sys.argv',['manage_document_storage.py','--apply'])
    with pytest.raises(SystemExit) as exc:manage_document_storage.main()
    assert exc.value.code==2


def test_unknown_or_unconfigured_backend_is_not_silently_local(monkeypatch):
    monkeypatch.setattr(settings,'storage_type','s3');monkeypatch.setattr(settings,'s3_bucket',None)
    response=client.post('/api/documents/upload?auto_process=false',files={'file':('i.pdf',b'x','application/pdf')})
    assert response.status_code==503
    assert client.get('/api/documents').json()['total']==0


def test_metadata_read_never_fetches_original_or_crops_and_cannot_be_saved(monkeypatch,isolated_document_database):
    from sqlalchemy import event
    id=upload();statements=[]
    event.listen(isolated_document_database.kw['bind'],'before_cursor_execute',lambda c,cu,s,p,ctx,m:statements.append(s))
    monkeypatch.setattr('app.services.storage.objects.read_object',lambda *a: (_ for _ in ()).throw(AssertionError('metadata must not read files')))
    metadata=DocumentRepository().get(id,load_sources=False)
    assert 'content' not in metadata and metadata['_metadata_only']
    assert not any('source_content' in s for s in statements)
    with pytest.raises(ValueError,match='full document'):DocumentRepository().save(metadata)
    assert client.get('/api/documents/'+id).status_code==200
    assert client.get('/api/documents/'+id+'/recovery').status_code==200
