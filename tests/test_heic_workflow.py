import base64
import io
import uuid
import zipfile
from types import SimpleNamespace
from pathlib import Path

import pytest
from PIL import Image
from pillow_heif import register_heif_opener

from app.core.config import settings
from app.services.extraction.heic import HEICImageError, heic_to_png, is_heic
from app.services.extraction.extractor import DocumentExtractor
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import client, restart_request, FakeQBO
from tests.test_web_review_flow import review_extractor

register_heif_opener()


def make_heic(mode="RGB", multi=False, orientation=None):
    image = Image.new(mode, (32, 24), (200, 50, 40, 0) if mode == "RGBA" else (200, 50, 40))
    output = io.BytesIO(); options = {}
    if multi:options.update(save_all=True, append_images=[Image.new("RGB", (16, 24), "blue")])
    if orientation:
        exif = image.getexif(); exif[274] = orientation; exif[315] = "private test metadata"; options["exif"] = exif
    image.save(output, format="HEIF", quality=-1, **options)
    return output.getvalue()


@pytest.fixture
def heic():
    return make_heic()


def upload_heic(content):
    result = client.post('/api/documents/upload?auto_process=false', files={'file':('phone.heic',content,'image/heic')})
    assert result.status_code == 200, result.text
    return result.json()['id']


def test_real_heic_conversion_returns_readable_rgb_png_without_source_metadata(heic):
    assert is_heic(heic)
    result = heic_to_png(heic)
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == 'PNG' and image.mode == 'RGB' and image.size == (32,24)
        assert not image.getexif()
        assert image.getpixel((10,10))[0] > 180


def test_alpha_is_composited_on_white():
    result = heic_to_png(make_heic('RGBA'))
    with Image.open(io.BytesIO(result)) as image:assert image.getpixel((5,5)) == (255,255,255)


def test_multi_image_heic_is_rejected_instead_of_dropping_other_images():
    with pytest.raises(HEICImageError, match='multiple images'):heic_to_png(make_heic(multi=True))


def test_informational_exif_does_not_rotate_heif_a_second_time():
    source = make_heic(orientation=6)
    with Image.open(io.BytesIO(heic_to_png(source))) as image:
        # HEIF uses container transforms; EXIF orientation is descriptive.
        assert image.size == (32,24)
        assert not image.getexif() and not image.info.get('exif')


@pytest.mark.parametrize('bad',[b'',b'not an image',b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1'])
def test_corrupt_input_has_a_clear_error(bad):
    with pytest.raises(HEICImageError):heic_to_png(bad)


def test_pixel_limit_is_checked_before_full_image_processing(heic,monkeypatch):
    monkeypatch.setattr(settings,'max_heic_pixels',32*24-1)
    with pytest.raises(HEICImageError,match='MAX_HEIC_PIXELS'):heic_to_png(heic)


def test_source_size_limit(heic,monkeypatch):
    monkeypatch.setattr(settings,'max_upload_bytes',len(heic)-1)
    with pytest.raises(HEICImageError,match='source exceeds'):heic_to_png(heic)


def test_normalized_output_size_limit(heic,monkeypatch):
    class TinyBuffer(io.BytesIO):
        def getvalue(self):return b'x' * (settings.max_upload_bytes+1)
    # Patch output construction only; retain an ordinary buffer for the decoder input.
    import app.services.extraction.heic as module
    real=io.BytesIO
    monkeypatch.setattr(module.io,'BytesIO',lambda *args:real(*args) if args else TinyBuffer())
    with pytest.raises(HEICImageError,match='Converted HEIC exceeds'):heic_to_png(heic)


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_both_ai_providers_receive_png_not_heic_labelled_as_png(heic,provider):
    captured=[]; extractor=DocumentExtractor.__new__(DocumentExtractor);extractor.provider=provider;extractor.model='test-model'
    def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))],content=[SimpleNamespace(text='ok')])
    extractor.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),messages=SimpleNamespace(create=create))
    assert extractor._vision_request(heic,'read document') == 'ok'
    blocks=captured[0]['messages'][0]['content']
    if provider=='openai':encoded=next(b['image_url']['url'].split(',',1)[1] for b in blocks if b['type']=='image_url')
    else:
        source=next(b['source'] for b in blocks if b['type']=='image');assert source['media_type']=='image/png';encoded=source['data']
    assert base64.b64decode(encoded).startswith(b'\x89PNG\r\n\x1a\n')


@pytest.mark.parametrize('provider',['openai','anthropic'])
def test_corrupt_heic_never_calls_provider(provider):
    extractor=DocumentExtractor.__new__(DocumentExtractor);extractor.provider=provider
    with pytest.raises(HEICImageError):extractor._vision_request(b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1','test')


def test_extraction_preview_source_and_restart(heic,review_extractor,monkeypatch,isolated_document_database):
    calls=[]
    def classify(self,image):
        assert image.startswith(b'\x89PNG');calls.append(image);return self.document_type
    monkeypatch.setattr(review_extractor,'classify_document',classify)
    id=upload_heic(heic);before=DocumentRepository().get(id);path='/api/documents/'+id
    preview=client.get(path+'/source/preview');assert preview.status_code==200,preview.text
    assert preview.headers['content-type']=='image/png' and preview.headers['cache-control']=='no-store'
    assert DocumentRepository().get(id)['_revision']==before['_revision']
    assert client.get(path+'/source').content==heic
    extracted=client.post(path+'/extract');assert extracted.status_code==200 and extracted.json()['status']=='extracted',extracted.text
    assert calls and extracted.json()['validation']['is_valid'] and extracted.json()['approved'] is False
    assert DocumentRepository().get(id)['content']==heic
    import os, subprocess, sys, json, hashlib
    env=os.environ.copy();env['DATABASE_URL']=str(isolated_document_database.kw['bind'].url)
    program = """import json,hashlib
from fastapi.testclient import TestClient
from app.main import app
from tests.auth_helpers import override_authenticated_api
override_authenticated_api(app)
c=TestClient(app)
r=c.get(PATH+'/source/preview');source=c.get(PATH+'/source')
print(json.dumps({'status':r.status_code,'png':r.content.startswith(bytes.fromhex('89504e470d0a1a0a')),'source_sha':hashlib.sha256(source.content).hexdigest()}))
""".replace('PATH',repr(path))
    result=subprocess.run([sys.executable,'-c',program],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,check=True,timeout=30)
    restored=json.loads(result.stdout)
    assert restored=={'status':200,'png':True,'source_sha':hashlib.sha256(heic).hexdigest()}


def test_invalid_labelled_heic_fails_before_ai_and_keeps_source(review_extractor,monkeypatch):
    def forbidden(*args):raise AssertionError('AI must not run')
    monkeypatch.setattr(review_extractor,'classify_document',forbidden)
    id=upload_heic(b'corrupt-heic');path='/api/documents/'+id
    result=client.post(path+'/extract');assert result.json()['status']=='failed'
    assert 'Invalid HEIC' in client.get(path).json()['error']
    assert client.get(path+'/source/preview').status_code==422
    assert client.get(path+'/source').content==b'corrupt-heic'
    assert not DocumentRepository().get(id)['approved']


@pytest.mark.parametrize('zip_upload',[False,True])
def test_bulk_and_zip_keep_original_heic(heic,zip_upload):
    from manage_users import initialize_company
    initialize_company('default','Test company')
    headers={'Idempotency-Key':str(uuid.uuid4())}
    if zip_upload:
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:archive.writestr('phone.heic',heic)
        result=client.post('/api/batch/upload-zip',headers=headers,files={'file':('images.zip',buffer.getvalue(),'application/zip')})
    else:result=client.post('/api/batch/upload',headers=headers,files={'files':('phone.heic',heic,'image/heic')})
    assert result.status_code==200,result.text
    id=result.json()['uploaded_documents'][0]
    assert client.get(f'/api/documents/{id}/source').content==heic
    assert client.get(f'/api/documents/{id}/source/preview').status_code==200


def test_qbo_attachment_uses_persisted_png_and_keeps_original_heic(heic,review_extractor,monkeypatch):
    id=upload_heic(heic);path='/api/documents/'+id
    assert client.post(path+'/extract').json()['status']=='extracted'
    assert client.post(path+'/approve').status_code==200
    fake=FakeQBO(id)
    original=fake.upload_attachment
    def attach(**kwargs):
        assert kwargs['file_name']=='phone.png' and kwargs['content_type']=='image/png'
        from app.services.persistence.attachment_sources import invoice_attachment
        from pathlib import Path
        doc=DocumentRepository().get(id)
        expected=invoice_attachment(doc)[2]
        assert Path(kwargs['file_path']).read_bytes()==expected and expected.startswith(b'\x89PNG')
        assert doc['content']==heic
        fake.attachments+=1
        return {'id':'heic-png-attachment'}
    monkeypatch.setattr(fake,'upload_attachment',attach)
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    result=client.post(path+'/push-to-qbo',json={'vendor_id':'30','account_id':'10','attach_documents':True})
    assert result.status_code==200 and result.json()['success'],result.text
    assert fake.writes==fake.attachments==1
    assert client.post(path+'/push-to-qbo',json={'vendor_id':'30','account_id':'10'}).status_code==409
    assert client.get(path+'/source').content==heic


def test_foreign_company_cannot_read_heic_preview(heic):
    from dataclasses import replace
    from app.main import app
    from app.core.auth import get_current_user
    from tests.auth_helpers import TEST_PRINCIPAL
    from app.services.persistence.auth_repository import company_uuid
    id=upload_heic(heic)
    async def other():return replace(TEST_PRINCIPAL,company_key='other',company_id=company_uuid('other'))
    app.dependency_overrides[get_current_user]=other
    assert client.get(f'/api/documents/{id}/source/preview').status_code==404


def test_preview_rejects_non_heic_source():
    result=client.post('/api/documents/upload?auto_process=false',files={'file':('old.png',b'png','image/png')})
    assert client.get('/api/documents/'+result.json()['id']+'/source/preview').status_code==415
    assert client.get('/api/documents/missing/source/preview').status_code==404

from tests.test_batch_extraction import actor


def test_durable_batch_worker_receives_png_and_retains_original(actor,heic,review_extractor,monkeypatch):
    from app.services.persistence.batch_extraction import run_once
    calls=[]
    def classify(self,image):
        assert image.startswith(b'\x89PNG');calls.append(image);return self.document_type
    monkeypatch.setattr(review_extractor,'classify_document',classify)
    uploaded=client.post('/api/batch/upload',headers={'Idempotency-Key':str(uuid.uuid4())},files={'files':('queued.heic',heic,'image/heic')}).json()
    batch_id=uploaded['batch_id'];id=uploaded['uploaded_documents'][0]
    queued=client.post(f'/api/batch/{batch_id}/extract',headers={'Idempotency-Key':str(uuid.uuid4())},json={})
    assert queued.status_code==202,queued.text
    result=run_once(queued.json()['run_id'])
    assert result['status']=='completed' and calls
    assert DocumentRepository().get(id)['content']==heic and not DocumentRepository().get(id)['approved']


def test_missing_decoder_reports_install_instruction(heic,monkeypatch):
    import builtins
    original=builtins.__import__
    def blocked(name,*args,**kwargs):
        if name=='pillow_heif':raise ImportError('test: dependency unavailable')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',blocked)
    with pytest.raises(HEICImageError,match='install the project requirements'):heic_to_png(heic)


def test_multi_image_source_cannot_reach_ai(review_extractor,monkeypatch):
    def forbidden(*args):raise AssertionError('AI must not run for a multi-image HEIC')
    monkeypatch.setattr(review_extractor,'classify_document',forbidden)
    source=make_heic(multi=True);id=upload_heic(source);path='/api/documents/'+id
    assert client.post(path+'/extract').json()['status']=='failed'
    assert 'multiple images' in client.get(path).json()['error']
    assert client.get(path+'/source/preview').status_code==422
    assert client.get(path+'/source').content==source

from tests.test_qbo_recovery import RecoveryQBO, request as recover


class HEICRecoveryQBO(RecoveryQBO):
    def get_vendors(self):return [{'id':'30','name':'Books by Bessie'}]
    def get_expense_accounts(self):return [{'id':'10','name':'Dues & Subscriptions','type':'Expense'}]
    def create_bill(self,**kwargs):
        self.writes+=1
        self.transactions['701']={'id':'701','type':'bill','date':kwargs['date'],'amount':str(kwargs['amount']),
            'vendor_id':kwargs['vendor_id'],'currency':'USD','expense_accounts':[kwargs['account_id']],'marker':kwargs['recovery_marker']}
        doc=next(d for d in self.docs() if d.get('qbo_push_state')=='in_progress')
        assert doc['qbo_attachment_source']['content_type']=='image/png' if self.require_prepared else True
        return {'id':'701','type':'bill'}
    def docs(self):return [DocumentRepository().get(self.document_id)]
    require_prepared=True


def heic_invoice(heic,monkeypatch):
    id=upload_heic(heic);path='/api/documents/'+id
    assert client.post(path+'/extract').json()['status']=='extracted'
    assert client.post(path+'/approve').status_code==200
    fake=HEICRecoveryQBO();fake.document_id=id
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    return id,fake


def test_uncertain_png_attachment_is_adopted_without_reencoding_or_duplicate_write(heic,review_extractor,monkeypatch,isolated_document_database):
    from app.services.persistence.attachment_sources import invoice_attachment
    id,fake=heic_invoice(heic,monkeypatch);fake.fail_attachment=True
    result=client.post(f'/api/documents/{id}/push-to-qbo',json={'vendor_id':'30','account_id':'10'})
    assert result.status_code==200 and not result.json()['success'],result.text
    doc=DocumentRepository().get(id);record=doc['qbo_attachment_source'];png=invoice_attachment(doc)[2]
    assert doc['qbo_push_state']=='succeeded' and fake.writes==fake.uploads==1
    def forbidden(*args):raise AssertionError('Recovery must use persisted PNG, never re-encode')
    monkeypatch.setattr('app.services.extraction.heic.heic_to_png',forbidden)
    info=recover(id,'source','inspect');assert info.status_code==200,info.text
    assert info.json()['provider']['file_name']=='phone.png' and info.json()['provider']['size']==len(png)
    verified=recover(id,'source');assert verified.status_code==200,verified.text
    assert DocumentRepository().get(id)['qbo_attachment_id']=='901'
    assert fake.writes==fake.uploads==1
    assert DocumentRepository().get(id)['qbo_attachment_source']==record
    assert restart_request(isolated_document_database,'GET',f'/api/documents/{id}/recovery')['status']==200


def test_known_unstarted_png_attachment_retry_uses_the_persisted_copy(heic,review_extractor,monkeypatch):
    from app.services.persistence.attachment_sources import prepare_invoice_attachment, invoice_attachment
    id,fake=heic_invoice(heic,monkeypatch)
    doc=DocumentRepository().get(id);prepare_invoice_attachment(doc);DocumentRepository().save(doc)
    assert client.post(f'/api/documents/{id}/push-to-qbo',json={'vendor_id':'30','account_id':'10','attach_documents':False}).json()['success']
    original=fake.upload_attachment;png=invoice_attachment(DocumentRepository().get(id))[2]
    def upload(**kwargs):
        assert kwargs['file_name']=='phone.png' and kwargs['content_type']=='image/png'
        assert Path(kwargs['file_path']).read_bytes()==png
        return original(**kwargs)
    monkeypatch.setattr(fake,'upload_attachment',upload)
    result=recover(id,'source','retry',acknowledged=True);assert result.status_code==200,result.text
    assert fake.writes==fake.uploads==1
    assert recover(id,'source','retry',acknowledged=True).status_code==200
    assert fake.uploads==1


def test_invalid_heic_cannot_create_bill_before_attachment_conversion(monkeypatch,review_extractor):
    id=upload_heic(b'bad HEIC');doc=DocumentRepository().get(id)
    doc.update(status='extracted',document_type='invoice',approved=True,validation={'is_valid':True},extracted_data=review_extractor().extract_receipt(b'image').model_dump())
    DocumentRepository().save(doc);fake=FakeQBO(id);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    result=client.post(f'/api/documents/{id}/push-to-qbo',json={'vendor_id':'30','account_id':'10'})
    assert result.status_code==409,result.text
    assert fake.writes==fake.attachments==0 and not DocumentRepository().get(id).get('qbo_push_context')


def test_corrupt_persisted_png_blocks_recovery(heic,review_extractor,monkeypatch):
    id,fake=heic_invoice(heic,monkeypatch);fake.fail_attachment=True
    client.post(f'/api/documents/{id}/push-to-qbo',json={'vendor_id':'30','account_id':'10'})
    doc=DocumentRepository().get(id);doc['qbo_attachment_source']['data_base64']='invalid';DocumentRepository().save(doc)
    assert recover(id,'source','inspect').status_code==409
    assert fake.writes==fake.uploads==1


def test_plan_preview_names_png_attachment_without_persisting_a_write(heic,review_extractor,monkeypatch):
    id,fake=heic_invoice(heic,monkeypatch);before=DocumentRepository().get(id)
    response=client.post(f'/api/qbo/invoice-plan/{id}',json={'create_as':'bill','vendor_id':'30','account_id':'10'})
    assert response.status_code==200,response.text
    info=response.json()['attachment'];assert info['file_name']=='phone.png' and info['converted_from']=='image/heic'
    assert DocumentRepository().get(id)==before and fake.writes==fake.uploads==0
