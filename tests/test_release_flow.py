import copy
import io
from pathlib import Path
from types import SimpleNamespace
import threading

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import documents as api
from app.core import database
from app.core.config import settings
from app.main import app
from app.services.extraction.extractor import DocumentExtractor
from app.services.persistence.check_sources import check_image_bytes, CheckSourceError
from app.services.persistence.repository import DocumentRepository
from manage_check_sources import preserve
from tests.test_document_api_persistence import client, prepare_bank_workflow, restart_request, seed_invoice, FakeQBO
from tests.test_qbo_recovery import bank_setup, request
from tests.test_web_review_flow import ReviewExtractor, review_extractor
from tests.test_company_access import tenants, signed_in


def test_check_snapshot_survives_deleted_file_and_process_restart(tmp_path,monkeypatch,isolated_document_database):
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    document=DocumentRepository().get(id)
    image_path=document['extracted_data']['check_images'][0]['image_path']
    Path(image_path).unlink()
    assert check_image_bytes(document,image_path)==b'check-image'
    import os,subprocess,sys,base64,json
    env=os.environ.copy();env['DATABASE_URL']=str(isolated_document_database.kw['bind'].url)
    program='from fastapi.testclient import TestClient\nfrom app.main import app\nfrom tests.auth_helpers import override_authenticated_api\nimport sys,base64\noverride_authenticated_api(app)\nr=TestClient(app).get(sys.argv[1])\nassert r.status_code==200,r.text\nprint(base64.b64encode(r.content).decode())\n'
    result=subprocess.run([sys.executable,'-c',program,f'/api/documents/{id}/checks/0/source'],env=env,
                          cwd=Path(__file__).resolve().parents[1],text=True,capture_output=True,check=True,timeout=30)
    assert base64.b64decode(result.stdout.strip())==b'check-image'

    response=client.get(f'/api/documents/{id}/checks/0/source')
    assert response.content==b'check-image' and response.headers['content-disposition']=="inline; filename*=UTF-8''check.png"


def test_bank_push_uses_database_copy_without_source_file(tmp_path,monkeypatch):
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    document=DocumentRepository().get(id);Path(document['extracted_data']['check_images'][0]['image_path']).unlink()
    original=fake.upload_attachment
    uploaded=[]
    def upload(**kwargs):
        uploaded.append(kwargs['file_path'])
        assert Path(kwargs['file_path']).read_bytes()==b'check-image'
        assert kwargs['file_name']=='check.png'
        return original(**kwargs)
    fake.upload_attachment=upload
    response=client.post(f'/api/documents/{id}/push-to-qbo',json={'attach_documents':True})
    assert response.status_code==200 and response.json()['success'] is True
    assert fake.category_writes==1 and fake.check_uploads==1
    assert DocumentRepository().get(id)['status']=='pushed'
    assert not Path(uploaded[0]).exists()


def test_missing_legacy_check_blocks_category_write(tmp_path,monkeypatch):
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    document=DocumentRepository().get(id);Path(document['extracted_data']['check_images'][0]['image_path']).unlink()
    document.pop('check_image_sources');DocumentRepository().save(document)
    assert client.post(f'/api/documents/{id}/push-to-qbo',json={'attach_documents':True}).status_code==409
    assert fake.category_writes==fake.check_uploads==0


def test_corrupt_check_does_not_fall_back_to_untrusted_local_file(tmp_path,monkeypatch):
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    document=DocumentRepository().get(id);path=document['extracted_data']['check_images'][0]['image_path']
    document['check_image_sources'][path]['sha256']='wrong';DocumentRepository().save(document)
    assert client.get(f'/api/documents/{id}/checks/0/source').status_code==409
    assert client.post(f'/api/documents/{id}/push-to-qbo',json={'attach_documents':True}).status_code==409
    assert fake.category_writes==fake.check_uploads==0


def test_recovery_upload_uses_persisted_check_without_original_file(tmp_path,monkeypatch):
    id,fake=bank_setup(tmp_path,monkeypatch,state='pushed')
    document=DocumentRepository().get(id);Path(document['check_match_results'][0]['image_path']).unlink()
    assert request(id,'check:0','retry',acknowledged=True).status_code==200
    assert fake.uploads==1 and DocumentRepository().get(id)['status']=='pushed'


def test_legacy_preservation_is_repeatable_and_keeps_write_state(tmp_path,monkeypatch,isolated_document_database):
    from sqlalchemy import select
    from app.models.database import Document
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    with isolated_document_database.begin() as session:
        row=session.scalar(select(Document).where(Document.api_id==id));state=copy.deepcopy(row.workflow_state);state.pop('check_image_sources');row.workflow_state=state
    first=preserve(apply=True);second=preserve(apply=True)
    assert first['new_copies']==1 and second['new_copies']==0
    assert first['missing_or_invalid']==second['missing_or_invalid']==0
    assert DocumentRepository().get(id)['category_suggestions'][0]['review_status']=='approved'
    assert fake.category_writes==fake.check_uploads==0


@pytest.mark.parametrize('index',[-1,1])
def test_unknown_check_index_is_not_a_file_path(tmp_path,monkeypatch,index):
    id,_=prepare_bank_workflow(tmp_path,monkeypatch)
    assert client.get(f'/api/documents/{id}/checks/{index}/source').status_code==404


def test_upload_limits_precede_database_and_processing(monkeypatch):
    monkeypatch.setattr(settings,'max_upload_bytes',4)
    assert client.post('/api/documents/upload?auto_process=false',files={'file':('x.pdf',b'12345','application/pdf')}).status_code==413
    assert client.post('/api/documents/upload?auto_process=false',files={'file':('x.pdf',b'','application/pdf')}).status_code==400
    assert client.get('/api/documents').json()['total']==0
    assert client.post('/api/documents/upload?auto_process=false',files={'file':('x.pdf',b'1234','application/pdf')}).status_code==200


def test_pdf_limit_prevents_rendering(monkeypatch):
    import app.services.extraction.extractor as module
    class Pdf:
        closed=False
        def __len__(self):return 2
        def __getitem__(self,index):raise AssertionError('must not render')
        def close(self):self.closed=True
    value=Pdf();monkeypatch.setattr(module.pdfium,'PdfDocument',lambda data:value);monkeypatch.setattr(settings,'max_pdf_pages',1)
    with pytest.raises(ValueError,match='MAX_PDF_PAGES'):
        DocumentExtractor.__new__(DocumentExtractor).pdf_to_page_images(b'pdf')
    assert value.closed


def test_invoice_partial_attachment_is_not_reported_as_full_success(monkeypatch):
    doc=seed_invoice();fake=FakeQBO(doc['id'],fail_attachment=True)
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    response=client.post(f"/api/documents/{doc['id']}/push-to-qbo",json={'vendor_id':'30','account_id':'10','attach_documents':True})
    assert response.status_code==200 and response.json()['success'] is False
    assert response.json()['transactions_pushed']==1
    assert DocumentRepository().get(doc['id'])['qbo_transaction_id']=='146'
    assert fake.writes==1 and fake.attachments==1


def test_readiness_requires_migrated_database(monkeypatch,tmp_path):
    assert client.get('/ready').json()=={'status':'ready'}
    engine=create_engine('sqlite:///'+str(tmp_path/'empty.db'));factory=sessionmaker(bind=engine)
    monkeypatch.setattr(database,'SessionLocal',factory)
    response=client.get('/ready');assert response.status_code==503
    assert 'sqlite' not in response.text and str(tmp_path) not in response.text
    assert client.get('/health').status_code==200
    engine.dispose()


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_jpeg_payload_uses_real_media_type(provider):
    value=DocumentExtractor.__new__(DocumentExtractor);value.provider=provider;value.model='test'
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))]) if provider=='openai' else SimpleNamespace(content=[SimpleNamespace(text='ok')])
    value.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),messages=SimpleNamespace(create=create))
    data=io.BytesIO();Image.new('RGB',(20,20),'white').save(data,format='JPEG')
    assert value._vision_request(data.getvalue(),'test')=='ok'
    content=calls[0]['messages'][0]['content'][-1]
    if provider=='openai':assert content['image_url']['url'].startswith('data:image/jpeg;base64,')
    else:assert content['source']['media_type']=='image/jpeg'


@pytest.mark.asyncio
async def test_extraction_does_not_block_other_requests(review_extractor,monkeypatch):
    import asyncio
    started=threading.Event();release=threading.Event()
    original=ReviewExtractor.classify_document
    def classify(self,image):
        started.set();assert release.wait(5);return original(self,image)
    monkeypatch.setattr(ReviewExtractor,'classify_document',classify)
    id=client.post('/api/documents/upload?auto_process=false',files={'file':('x.png',b'image','image/png')}).json()['id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as session:
        task=asyncio.create_task(session.post(f'/api/documents/{id}/extract'))
        try:
            assert await asyncio.to_thread(started.wait,2)
            assert (await asyncio.wait_for(session.get('/api/documents'),1)).status_code==200
            assert (await session.post(f'/api/documents/{id}/extract')).status_code==409
        finally:release.set()
        assert (await task).status_code==200


@pytest.mark.auth_live
def test_persisted_check_source_is_scoped_to_real_login(tenants,tmp_path):
    path=tmp_path/'check.png';path.write_bytes(b'private-check')
    doc=DocumentRepository().get('doc_default')
    doc.update(document_type='bank_statement',status='extracted',extracted_data={'check_images':[{'image_path':str(path)}]})
    DocumentRepository().save(doc);path.unlink()
    a=signed_in(tenants['a']['email']);b=signed_in(tenants['b']['email'])
    assert a.get('/api/documents/doc_default/checks/0/source').content==b'private-check'
    assert b.get('/api/documents/doc_default/checks/0/source').status_code==404
    from fastapi.testclient import TestClient
    assert TestClient(app).get('/api/documents/doc_default/checks/0/source').status_code==401


def test_interrupted_extraction_requires_explicit_operator_recovery():
    from manage_extraction import recover_interrupted
    doc=seed_invoice();doc.update(status='classifying',extraction_claim='interrupted');DocumentRepository().save(doc)
    assert client.post(f"/api/documents/{doc['id']}/extract").status_code==409
    recover_interrupted(doc['id'])
    state=DocumentRepository().get(doc['id'])
    assert state['status']=='failed' and 'extraction_claim' not in state
    doc=state;doc.update(status='extracting',extraction_claim='interrupted',qbo_push_state='uncertain');DocumentRepository().save(doc)
    with pytest.raises(Exception):recover_interrupted(doc['id'])
    assert DocumentRepository().get(doc['id'])['qbo_push_state']=='uncertain'


def test_extraction_retry_replaces_check_snapshot_and_old_reviews(tmp_path,monkeypatch):
    from app.schemas.documents import DocumentType,BankStatementData,CheckImage
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    doc=DocumentRepository().get(id);path=doc['extracted_data']['check_images'][0]['image_path']
    doc.update(status='failed',content_type='image/png');DocumentRepository().save(doc)
    class Extractor(ReviewExtractor):
        document_type=DocumentType.BANK_STATEMENT
        def extract_bank_statement(self,**kwargs):
            Path(path).write_bytes(b'new-check')
            return BankStatementData(transactions=[],check_images=[CheckImage(image_path=path)])
    monkeypatch.setattr('app.services.extraction.extractor.DocumentExtractor',Extractor)
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    response=client.post(f'/api/documents/{id}/extract')
    assert response.status_code==200 and response.json()['status']=='extracted'
    state=DocumentRepository().get(id)
    assert check_image_bytes(state,path)==b'new-check'
    assert 'category_suggestions' not in state and 'qbo_match_results' not in state
    assert fake.category_writes==fake.check_uploads==0


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_configured_ai_model_is_used(provider,monkeypatch):
    import openai,anthropic
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace())
    monkeypatch.setattr(anthropic,'Anthropic',lambda **kwargs:SimpleNamespace())
    monkeypatch.setattr(settings,'ai_model','custom-vision-model')
    assert DocumentExtractor(api_key='test',provider=provider).model=='custom-vision-model'


def test_selected_provider_wins_when_both_keys_exist(monkeypatch):
    selected=[]
    class Extractor(ReviewExtractor):
        def __init__(self,**kwargs):selected.append(kwargs['provider'])
    monkeypatch.setattr('app.services.extraction.extractor.DocumentExtractor',Extractor)
    monkeypatch.setenv('OPENAI_API_KEY','test-openai')
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-anthropic')
    monkeypatch.setattr(settings,'ai_provider','anthropic')
    id=client.post('/api/documents/upload?auto_process=false',files={'file':('image.png',b'image','image/png')}).json()['id']
    assert client.post(f'/api/documents/{id}/extract').json()['status']=='extracted'
    assert selected==['anthropic']
