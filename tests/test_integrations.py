import base64
import hashlib
import hmac
import json
import socket
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core import database
from app.core.auth import get_current_user
from app.core.config import settings
from app.main import app
from app.models.database import (Document, UploadBatch, User, WebhookIntegration, WebhookReceipt,
    NotificationPreference, NotificationOutbox, BatchExtractionRun)
from app.services.integrations import webhooks as wh, notifications as notices
from app.services.persistence.batch_extraction import run_once
from app.services.persistence.repository import DocumentRepository
from tests.test_batch_extraction import actor, fake_extraction

client = TestClient(app)


@pytest.fixture
def integration(actor, monkeypatch):
    monkeypatch.setattr(settings, 'qbo_token_encryption_key', Fernet.generate_key().decode())
    result = client.post('/api/integrations/webhooks', json={'name': 'Test adapter', 'auto_extract': False})
    assert result.status_code == 201, result.text
    return result.json()


def body(kind='generic'):
    data = {'attachments': [{'filename': 'invoice.png', 'content_type': 'image/png',
                            'content_base64': base64.b64encode(b'private-source-content').decode()}]}
    return json.dumps({'event': 'document.uploaded', 'data': data} if kind == 'generic' else data, separators=(',', ':')).encode()


def headers(integration, raw, kind='generic', event=None, stamp=None):
    stamp, event = str(stamp or int(time.time())), event or str(uuid.uuid4())
    signature = hmac.new(integration['secret'].encode(), f'{stamp}.{event}.{kind}.'.encode() + raw, hashlib.sha256).hexdigest()
    return {'X-Signature': signature, 'X-Webhook-Id': event, 'X-Webhook-Timestamp': stamp}


def receive(integration, raw=None, kind='generic', signed=None):
    raw = raw if raw is not None else body(kind)
    return client.post(f"/api/webhooks/{integration['id']}/{kind}", content=raw, headers=signed or headers(integration, raw, kind))


def count(model):
    with database.SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize('kind', ['generic', 'email', 'zapier'])
def test_signed_replay_and_private_source(integration, kind):
    raw=body(kind);signed=headers(integration,raw,kind)
    first=receive(integration,raw,kind,signed)
    assert first.status_code==202,first.text
    assert receive(integration,raw,kind,headers(integration,raw,kind,signed['X-Webhook-Id'])).json()==first.json()
    assert count(Document)==count(UploadBatch)==count(WebhookReceipt)==1
    assert count(BatchExtractionRun)==0
    with database.SessionLocal() as session:
        id=session.get(UploadBatch,first.json()['batch_id']).document_ids[0]
        assert session.get(WebhookIntegration,integration['id']).secret!=integration['secret']
        row=session.scalar(select(Document))
        assert row.source_content is None and row.workflow_state['approved'] is False
    assert client.get(f'/api/documents/{id}/source').content==b'private-source-content'
    assert integration['secret'] not in client.get('/api/integrations/webhooks').text
    assert 'private-source-content' not in client.get(f"/api/integrations/webhooks/{integration['id']}/events").text


@pytest.mark.parametrize('bad',['missing','signature','timestamp','event','expired','future','path','body'])
def test_signature_checked_before_payload(integration,monkeypatch,bad):
    raw=body();signed=headers(integration,raw)
    if bad=='missing':signed={'X-Signature':''}
    elif bad=='signature':signed['X-Signature']='0'*64
    elif bad=='timestamp':signed['X-Webhook-Timestamp']='not-time'
    elif bad=='event':signed['X-Webhook-Id']='not-uuid'
    elif bad in {'expired','future'}:signed=headers(integration,raw,stamp=int(time.time())+(-301 if bad=='expired' else 301))
    elif bad=='path':signed=headers(integration,raw,'email')
    elif bad=='body':raw+=b' '
    monkeypatch.setattr(wh,'prepare',lambda *a:pytest.fail('Payload before signature'))
    assert receive(integration,raw,signed=signed).status_code==401
    assert count(Document)==count(WebhookReceipt)==0


def test_event_body_conflict_preserves_original(integration):
    raw=body();signed=headers(integration,raw)
    first=receive(integration,raw,signed=signed).json()
    changed=raw.replace(b'invoice.png',b'another.png')
    assert receive(integration,changed,signed=headers(integration,changed,event=signed['X-Webhook-Id'])).status_code==409
    assert count(Document)==1
    assert client.get(f"/api/integrations/webhooks/{integration['id']}/events").json()['items'][0]==first


@pytest.mark.parametrize('change',['qbo','extraction','company','empty','path','mime','base64','both'])
def test_invalid_payload_never_mutates_workflow(integration,change):
    payload=json.loads(body());data=payload['data'];file=data['attachments'][0]
    if change=='qbo':payload['event']='qbo.synced'
    elif change=='extraction':payload['event']='extraction.completed'
    elif change=='company':payload['company_id']='beta'
    elif change=='empty':data['attachments']=[]
    elif change=='path':file['filename']='../x.png'
    elif change=='mime':file['content_type']='application/executable'
    elif change=='base64':file['content_base64']='%%%'
    else:file['url']='https://example.com/x'
    result=receive(integration,json.dumps(payload).encode())
    assert result.status_code in {413,422},result.text
    assert count(Document)==0
    with database.SessionLocal() as session:assert session.scalar(select(WebhookReceipt)).status=='retryable'


def test_body_file_limits(integration,monkeypatch):
    monkeypatch.setattr(wh,'MAX_BODY',10)
    assert receive(integration).status_code==413
    monkeypatch.setattr(wh,'MAX_BODY',10000)
    monkeypatch.setattr(settings,'max_upload_bytes',2)
    assert receive(integration).status_code==413
    assert count(Document)==0


def test_revocation_rotation(integration):
    raw=body();signed=headers(integration,raw)
    rotated=client.post(f"/api/integrations/webhooks/{integration['id']}/rotate").json()
    assert receive(integration,raw,signed=signed).status_code==401
    assert receive(rotated).status_code==202
    assert client.post(f"/api/integrations/webhooks/{integration['id']}/revoke").status_code==200
    assert receive(rotated).status_code==401
    assert client.post(f"/api/integrations/webhooks/{integration['id']}/rotate").status_code==409


def test_auto_extraction_durable_and_revocable(integration,monkeypatch):
    with database.SessionLocal() as session:
        session.get(WebhookIntegration,integration['id']).auto_extract=True;session.commit()
    raw=body();signed=headers(integration,raw)
    event=receive(integration,raw,signed=signed).json()
    assert event['run_id'] and count(BatchExtractionRun)==1
    assert receive(integration,raw,signed=signed).json()==event
    calls=fake_extraction(monkeypatch)
    assert run_once(event['run_id'])['status']=='completed' and len(calls)==1
    assert run_once(event['run_id']) is None
    next_event=receive(integration).json()
    client.post(f"/api/integrations/webhooks/{integration['id']}/revoke")
    assert run_once(next_event['run_id'])['status']=='cancelled' and len(calls)==1


@pytest.mark.parametrize('disable',['user','role'])
def test_owner_revoked_stops_queued_job(integration,actor,monkeypatch,disable):
    with database.SessionLocal() as session:
        session.get(WebhookIntegration,integration['id']).auto_extract=True;session.commit()
    result=receive(integration).json()
    with database.SessionLocal() as session:
        owner=session.get(User,actor.user_id)
        if disable=='user':owner.is_active=False
        else:owner.role='user'
        session.commit()
    monkeypatch.setattr('app.api.documents._process_document_sync',lambda *a,**k:pytest.fail('Revoked owner processed'))
    assert run_once(result['run_id'])['status']=='cancelled'
    assert receive(integration).status_code==401


@pytest.mark.parametrize('committed',[False,True])
def test_offline_receipt_recovery_no_duplicate(integration,monkeypatch,committed):
    from app.api import batch
    original=batch._persist_batch
    def crash(*a):
        if committed:original(*a)
        raise RuntimeError('simulated crash')
    monkeypatch.setattr(batch,'_persist_batch',crash)
    raw=body();signed=headers(integration,raw)
    with pytest.raises(RuntimeError):wh.receive(integration['id'],'generic',signed,raw)
    assert receive(integration,raw,signed=signed).status_code==409
    with database.SessionLocal() as session:id=session.scalar(select(WebhookReceipt.id))
    result=wh.recover_receipt(id)
    assert result['status']==('accepted' if committed else 'retryable')
    monkeypatch.setattr(batch,'_persist_batch',original)
    assert receive(integration,raw,signed=signed).status_code==202
    assert count(Document)==1


@pytest.mark.parametrize('url',['http://files.example.com/a','https://other.example.com/a','https://user:pass@files.example.com/a','https://files.example.com:444/a','https://files.example.com/a#f','https://127.0.0.1/a'])
def test_unapproved_urls_not_resolved(monkeypatch,url):
    monkeypatch.setattr(settings,'webhook_download_hosts','files.example.com')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:pytest.fail('Unapproved URL resolved'))
    with pytest.raises(HTTPException):wh.download(url,100)


@pytest.mark.parametrize('ips',[['127.0.0.1'],['169.254.169.254'],['10.0.0.1'],['::1'],['8.8.8.8','192.168.1.2'],['224.0.0.1'],['ff0e::1']])
def test_private_and_mixed_dns_rejected(monkeypatch,ips):
    monkeypatch.setattr(settings,'webhook_download_hosts','files.example.com')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(0,0,0,'',(ip,443)) for ip in ips])
    monkeypatch.setattr(wh,'PinnedHTTPS',lambda *a:pytest.fail('Private connection'))
    with pytest.raises(HTTPException):wh.download('https://files.example.com/a',100)


@pytest.mark.parametrize('status,encoding,data,success',[(200,'identity',b'abc',True),(302,'identity',b'abc',False),(200,'gzip',b'abc',False),(200,'identity',b'abcdef',False)])
def test_public_url_dns_pinning_and_bounds(monkeypatch,status,encoding,data,success):
    monkeypatch.setattr(settings,'webhook_download_hosts','files.example.com');calls=[]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(0,0,0,'',('8.8.8.8',443))])
    class Fake:
        def __init__(self,host,ip):calls.append((host,ip))
        def request(self,*a,**k):calls.append((a,k))
        def getresponse(self):return SimpleNamespace(status=status,getheader=lambda h,default=None:encoding if h=='Content-Encoding' else None,read=lambda n:data[:n])
        def close(self):calls.append('closed')
    monkeypatch.setattr(wh,'PinnedHTTPS',Fake)
    if success:assert wh.download('https://files.example.com/a?x=1',3)==b'abc'
    else:
        with pytest.raises(HTTPException):wh.download('https://files.example.com/a?x=1',3)
    assert calls[0]==('files.example.com','8.8.8.8') and calls[-1]=='closed'
    assert calls[1][1]['headers']['Host']=='files.example.com'


@pytest.fixture
def notices_ready(actor,monkeypatch):
    monkeypatch.setattr(settings,'email_enabled',True)
    monkeypatch.setattr(settings,'email_from','sender@example.com')
    monkeypatch.setattr(settings,'smtp_host','smtp.example.com')
    with database.SessionLocal() as session:
        session.get(User,actor.user_id).email='recipient@example.com'
        session.add(NotificationPreference(user_id=actor.user_id,company_id=actor.company_id,enabled=True,recipient_email='recipient@example.com'))
        session.commit()
    def queue(kind='extraction.succeeded',key=None,commit=True):
        with database.SessionLocal() as session:
            notices.queue_transition(session,actor.company_id,'doc_notice',key or str(uuid.uuid4()),kind,{'filename':'<private>.png'})
            if commit:session.commit()
    return queue


def test_notification_transaction_dedup_optin(notices_ready,actor):
    notices_ready(commit=False);assert count(NotificationOutbox)==0
    notices_ready(kind='attachment.uncertain',key='same');notices_ready(kind='attachment.uncertain',key='same');assert count(NotificationOutbox)==1
    with database.SessionLocal() as session:
        session.get(NotificationPreference,actor.user_id).enabled=False;session.commit()
    notices_ready();assert count(NotificationOutbox)==1


def test_disabled_channel_no_backlog(notices_ready,monkeypatch):
    monkeypatch.setattr(settings,'email_enabled',False);notices_ready()
    assert count(NotificationOutbox)==0 and notices.run_once() is None


def test_acceptance_once_and_uncertainty_never_retried(notices_ready,monkeypatch):
    calls=[]
    def send(row,configured):
        with database.SessionLocal() as session:assert session.get(NotificationOutbox,row.id).status=='sending'
        calls.append(row.id)
        if len(calls)==2:raise TimeoutError('response lost')
        return 'provider-123'
    monkeypatch.setattr(notices,'send',send)
    notices_ready(kind='category.uncertain');first=notices.run_once();assert notices.run_once() is None
    notices_ready();second=notices.run_once();assert notices.run_once() is None
    with database.SessionLocal() as session:
        assert session.get(NotificationOutbox,first).status=='accepted'
        assert session.get(NotificationOutbox,second).status=='uncertain'
    assert len(calls)==2
    assert client.post(f'/api/integrations/notifications/{second}/retry').status_code==409


@pytest.mark.parametrize('invalid',['optout','inactive','emailchange','companychange'])
def test_recipient_revalidated_before_send(notices_ready,actor,monkeypatch,invalid):
    notices_ready()
    with database.SessionLocal() as session:
        if invalid=='optout':session.get(NotificationPreference,actor.user_id).enabled=False
        elif invalid=='inactive':session.get(User,actor.user_id).is_active=False
        elif invalid=='emailchange':session.get(User,actor.user_id).email='changed@example.com'
        else:session.get(NotificationPreference,actor.user_id).company_id=str(uuid.uuid4())
        session.commit()
    monkeypatch.setattr(notices,'send',lambda *a:pytest.fail('Revoked recipient'))
    id=notices.run_once()
    with database.SessionLocal() as session:assert session.get(NotificationOutbox,id).status=='cancelled'


def test_config_failure_explicit_retry(notices_ready,monkeypatch):
    notices_ready();monkeypatch.setattr(settings,'email_from',None)
    monkeypatch.setattr(notices,'send',lambda *a:pytest.fail('Invalid config sent'))
    id=notices.run_once()
    with database.SessionLocal() as session:assert session.get(NotificationOutbox,id).status=='failed_before_send'
    assert notices.run_once() is None
    assert client.post(f'/api/integrations/notifications/{id}/retry').status_code==200
    assert client.post(f'/api/integrations/notifications/{id}/cancel').status_code==200


def test_crash_sending_offline_recovery(notices_ready,monkeypatch):
    notices_ready()
    def crash(*a):raise KeyboardInterrupt()
    monkeypatch.setattr(notices,'send',crash)
    with pytest.raises(KeyboardInterrupt):notices.run_once()
    assert notices.run_once() is None
    with database.SessionLocal() as session:id=session.scalar(select(NotificationOutbox.id))
    assert notices.recover_notification(id)['status']=='uncertain'
    assert notices.run_once() is None


@pytest.mark.parametrize('provider',['smtp','sendgrid','resend','postmark'])
def test_mock_provider_payload_tls_escape(notices_ready,monkeypatch,provider):
    notices_ready();monkeypatch.setattr(settings,'email_provider',provider)
    if provider!='smtp':monkeypatch.setattr(settings,provider+'_api_key','test-only-key')
    calls=[]
    class SMTP:
        def __init__(self,*a,**k):assert k['timeout']==15
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def starttls(self,**k):calls.append('tls');assert k['context']
        def send_message(self,message):
            assert '&lt;private&gt;' in message.get_payload()[1].get_content()
            calls.append('send');return {}
    class HTTP:
        def __init__(self,**k):assert k['trust_env'] is False and k['follow_redirects'] is False
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,url,**k):
            calls.append(url);payload=k['json']
            assert '<private>' not in str(payload.get('html',payload.get('HtmlBody','')))
            if provider=='resend':assert k['headers']['Idempotency-Key']
            return SimpleNamespace(status_code=202 if provider=='sendgrid' else 200,headers={'X-Message-Id':'sg-1'},
                raise_for_status=lambda:None,json=lambda:{'id':'r-1','MessageID':'p-1','ErrorCode':0})
    monkeypatch.setattr(notices.smtplib,'SMTP',SMTP);monkeypatch.setattr(notices.httpx,'Client',HTTP)
    id=notices.run_once()
    with database.SessionLocal() as session:assert session.get(NotificationOutbox,id).status=='accepted'
    assert calls==['tls','send'] if provider=='smtp' else len(calls)==1


def test_company_and_role_scope(integration,actor,notices_ready):
    notices_ready();receive(integration)
    async def foreign():return replace(actor,company_id=str(uuid.uuid4()),company_key='beta')
    app.dependency_overrides[get_current_user]=foreign
    assert client.get('/api/integrations/webhooks').json()['items']==[]
    assert client.get(f"/api/integrations/webhooks/{integration['id']}/events").status_code==404
    assert client.post(f"/api/integrations/webhooks/{integration['id']}/revoke").status_code==404
    assert client.get('/api/integrations/notifications').json()['items']==[]
    async def normal():return replace(actor,role='user')
    app.dependency_overrides[get_current_user]=normal
    assert client.get('/api/integrations/webhooks').status_code==403
    assert client.post('/api/integrations/webhooks',json={'name':'forbidden'}).status_code==403


def test_document_transition_queues_in_same_commit(integration,notices_ready):
    event=receive(integration).json()
    with database.SessionLocal() as session:id=session.get(UploadBatch,event['batch_id']).document_ids[0]
    doc=DocumentRepository().get(id)
    doc['extraction_claim']='claimed';doc['status']='extracting';DocumentRepository().save(doc)
    doc['extraction_claim']=None;doc['status']='extracted';DocumentRepository().save(doc)
    assert count(NotificationOutbox)==1
    DocumentRepository().save(doc);assert count(NotificationOutbox)==1


def test_concurrent_notifications_only_one_transport_attempt(notices_ready,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    notices_ready();calls=[];barrier=threading.Barrier(2);original=notices.configuration
    def config(recipient):
        result=original(recipient);barrier.wait(timeout=5);return result
    monkeypatch.setattr(notices,'configuration',config)
    monkeypatch.setattr(notices,'send',lambda row,c:(calls.append(row.id) or 'accepted-one'))
    with ThreadPoolExecutor(max_workers=2) as executor:list(executor.map(lambda _:notices.run_once(),range(2)))
    assert len(calls)==1
    with database.SessionLocal() as session:assert session.scalar(select(NotificationOutbox.status))=='accepted'


def test_webhook_records_restored_in_separate_process(integration,isolated_document_database,monkeypatch):
    import os
    import subprocess
    import sys
    result=receive(integration).json()
    env=os.environ.copy();env['DATABASE_URL']=str(isolated_document_database.kw['bind'].url)
    code="""
import json
from app.core import database
from app.models.database import WebhookReceipt,WebhookIntegration,UploadBatch
from app.services.integrations.webhooks import authenticate
from sqlalchemy import select
with database.SessionLocal() as s:
 r=s.scalar(select(WebhookReceipt));b=s.get(UploadBatch,r.batch_id);i=s.get(WebhookIntegration,r.integration_id)
 print(json.dumps({'event':r.event_id,'batch':b.id,'docs':b.document_ids,'active':i.enabled}))
"""
    child=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,check=True)
    saved=json.loads(child.stdout)
    assert saved['event']==result['event_id'] and saved['batch']==result['batch_id'] and saved['active']
    assert len(saved['docs'])==1


from tests.test_company_access import tenants, signed_in


@pytest.mark.auth_live
def test_live_cookie_csrf_roles_tenants_and_revoked_session(tenants):
    admin=signed_in(tenants['a']['email']);other=signed_in(tenants['b']['email']);normal=signed_in(tenants['user']['email'])
    anonymous=TestClient(app)
    assert anonymous.get('/api/integrations/webhooks').status_code==401
    assert anonymous.get('/api/integrations/notifications').status_code==401
    csrf=admin.headers.pop('X-CSRF-Token')
    assert admin.post('/api/integrations/webhooks',json={'name':'live'}).status_code==403
    assert admin.patch('/api/integrations/notifications',json={'enabled':True}).status_code==403
    admin.headers['X-CSRF-Token']=csrf
    assert admin.post('/api/integrations/webhooks',headers={'Origin':'https://evil.example'},json={'name':'live'}).status_code==403
    result=admin.post('/api/integrations/webhooks',json={'name':'live'});assert result.status_code==201,result.text
    item=result.json()
    assert other.post(f"/api/integrations/webhooks/{item['id']}/revoke").status_code==404
    assert normal.post(f"/api/integrations/webhooks/{item['id']}/revoke").status_code==403
    assert normal.patch('/api/integrations/notifications',json={'enabled':True}).status_code==200
    assert admin.get('/api/integrations/notifications').json()['enabled'] is False
    assert normal.get('/api/integrations/notifications').json()['enabled'] is True
    assert admin.post('/api/auth/logout').status_code==200
    assert admin.get('/api/integrations/webhooks').status_code==401
    # Signed ingress remains independent of an expired browser cookie.
    raw=body();assert anonymous.post(f"/api/webhooks/{item['id']}/generic",content=raw,headers=headers(item,raw)).status_code==202
    assert other.get('/api/documents').json()['total']==1  # only its fixture document
