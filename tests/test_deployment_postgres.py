"""Opt-in live PostgreSQL acceptance; only disposable isolated schemas are used.
Run in operator: -e DEPLOYMENT_TEST_POSTGRES=1 ... pytest this file.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.core.config import settings
from app.main import app
from app.models.database import (Base, Company, User, Document, ExtractedData, AuthLoginAttempt,
    WebhookIntegration, NotificationOutbox, NotificationPreference)
from app.services.deployment.transfer import transfer
from app.services.persistence.auth_repository import AuthRepository
from app.services.persistence.repository import DocumentRepository
from manage_users import initialize_company
from tests.test_deployment import stamp

pytestmark=[pytest.mark.auth_live, pytest.mark.skipif(os.getenv('DEPLOYMENT_TEST_POSTGRES')!='1',
    reason='Requires explicit PostgreSQL acceptance in the Docker operator service')]
ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def pg(monkeypatch):
    url=settings.database_url
    engine=create_engine(url)
    if engine.dialect.name!='postgresql':
        engine.dispose();pytest.fail('DEPLOYMENT_TEST_POSTGRES=1 requires a PostgreSQL DATABASE_URL')
    schema='acceptance_'+uuid.uuid4().hex
    with engine.begin() as conn:conn.execute(text('CREATE SCHEMA '+schema))
    target=create_engine(url,connect_args={'options':'-csearch_path='+schema})
    try:
        config=Config(str(ROOT/'alembic.ini'));config.set_main_option('script_location',str(ROOT/'migrations'))
        with target.begin() as conn:
            config.attributes['connection']=conn;command.upgrade(config,'head')
        monkeypatch.setattr(database,'engine',target)
        monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=target,autoflush=False))
        monkeypatch.setattr(settings,'qbo_token_encryption_key',Fernet.generate_key().decode())
        monkeypatch.setattr(settings,'email_enabled',False)
        yield target
    finally:
        target.dispose()
        with engine.begin() as conn:conn.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        engine.dispose()


def test_live_postgres_migrations_typed_transfer_and_sequence(pg,tmp_path):
    source=create_engine('sqlite:///'+str(tmp_path/'source.db'));Base.metadata.create_all(source);stamp(source)
    company,user,document=[str(uuid.uuid4()) for _ in range(3)]
    secret=Fernet(settings.qbo_token_encryption_key.encode()).encrypt(b'preserved-webhook-secret').decode()
    with source.begin() as conn:
        conn.execute(Company.__table__.insert().values(id=company,name='Transfer test',api_key='pg-copy'))
        conn.execute(User.__table__.insert().values(id=user,company_id=company,email='pg-copy@example.com',role='admin',is_active=True))
        conn.execute(Document.__table__.insert().values(id=document,company_id=company,api_id='doc_pg_copy',filename='original.pdf',
            content_type='application/pdf',source_content=b'%PDF-preserved-source',status='pushed',
            workflow_state={'id':'doc_pg_copy','company_id':'pg-copy','status':'pushed','approved':True,
                            'qbo_push_state':'succeeded','qbo_transaction_id':'existing-167','nested':{'currency':'USD','items':[1,2.5,None,False]}}))
        conn.execute(ExtractedData.__table__.insert().values(document_id=document,total_amount=Decimal('19.09'),date=datetime(2026,10,18)))
        conn.execute(AuthLoginAttempt.__table__.insert().values(id=50,subject_hash='a'*64,created_at=datetime(2026,10,18)))
        conn.execute(WebhookIntegration.__table__.insert().values(company_id=company,owner_user_id=user,name='Preserved',secret=secret,enabled=True,auto_extract=False))
        conn.execute(NotificationOutbox.__table__.insert().values(company_id=company,user_id=user,recipient_email='pg-copy@example.com',
            event_key='already-sent',kind='extraction.succeeded',document_id='doc_pg_copy',filename='original.pdf',status='accepted',provider_id='provider-existing'))
    try:
        report=transfer(source,pg,apply=True)
        assert report['mode']=='applied'
        with pg.begin() as conn:
            row=conn.execute(select(Document.__table__)).mappings().one()
            assert row['company_id']==company and row['source_content']==b'%PDF-preserved-source'
            assert row['workflow_state']['qbo_transaction_id']=='existing-167' and row['workflow_state']['nested']['items']==[1,2.5,None,False]
            assert conn.scalar(select(ExtractedData.total_amount))==Decimal('19.09')
            assert conn.scalar(select(WebhookIntegration.secret))==secret
            assert conn.scalar(select(NotificationOutbox.status))=='accepted'
            id=conn.execute(AuthLoginAttempt.__table__.insert().values(subject_hash='b'*64,created_at=datetime(2026,10,18)).returning(AuthLoginAttempt.id)).scalar_one()
            assert id>50
    finally:source.dispose()


def login(email):
    c=TestClient(app);result=c.post('/api/auth/login',json={'email':email,'password':'Strong-acceptance-password-114'})
    assert result.status_code==200,result.text
    c.headers['X-CSRF-Token']=c.get('/api/auth/me').json()['csrf_token'];return c


def test_live_postgres_tenants_storage_and_batch_worker(pg,monkeypatch):
    repo=AuthRepository()
    for key in ['default','other']:
        company=initialize_company(key,key)
        repo.create_user(company,key+'@example.com','Strong-acceptance-password-114',key,'admin')
    a,b=login('default@example.com'),login('other@example.com')
    result=a.post('/api/batch/upload',headers={'Idempotency-Key':str(uuid.uuid4())},
        files=[('files',('one.png',b'postgres-private-source','image/png'))]);assert result.status_code==200,result.text
    data=result.json();id=data['uploaded_documents'][0]
    assert a.get(f'/api/documents/{id}/source').content==b'postgres-private-source'
    assert b.get(f'/api/documents/{id}').status_code==404
    result=a.post(f"/api/batch/{data['batch_id']}/extract",headers={'Idempotency-Key':str(uuid.uuid4())},json={})
    assert result.status_code==202,result.text
    def fake(id,expected_revision=None):
        doc=DocumentRepository().get(id);assert doc['_revision']==expected_revision
        doc['status']='extracted';DocumentRepository().save(doc)
    monkeypatch.setattr('app.api.documents._process_document_sync',fake)
    from app.services.persistence.batch_extraction import run_once
    run=result.json()['run_id'];assert run_once(run)['status']=='completed' and run_once(run) is None
    assert a.get('/api/analytics/summary').status_code==200


def test_live_postgres_outbox_concurrent_claim(pg,monkeypatch):
    from app.services.integrations import notifications
    company=initialize_company('default','Company')
    user=AuthRepository().create_user(company,'recipient@example.com','Strong-acceptance-password-114','Tester','admin')
    with database.SessionLocal() as session:
        session.add(NotificationPreference(user_id=user['id'],company_id=company,enabled=True,recipient_email='recipient@example.com'))
        session.add(NotificationOutbox(user_id=user['id'],company_id=company,recipient_email='recipient@example.com',
            event_key='one',kind='extraction.succeeded',document_id='doc_test',filename='test.png'))
        session.commit()
    monkeypatch.setattr(settings,'email_enabled',True);monkeypatch.setattr(settings,'email_provider','smtp')
    monkeypatch.setattr(settings,'email_from','sender@example.com');monkeypatch.setattr(settings,'smtp_host','smtp.example.com')
    monkeypatch.setattr(settings,'smtp_username',None);monkeypatch.setattr(settings,'smtp_password',None)
    calls=[];monkeypatch.setattr(notifications,'send',lambda row,config:(calls.append(row.id) or 'mock-accepted'))
    with ThreadPoolExecutor(max_workers=2) as executor:list(executor.map(lambda _:notifications.run_once(),range(2)))
    assert len(calls)==1
    with database.SessionLocal() as session:assert session.scalar(select(NotificationOutbox.status))=='accepted'


def test_live_postgres_encrypted_backup_restore_hold(pg,tmp_path,monkeypatch):
    from app.services.deployment import backups,restore_guard
    from app.services.storage.objects import write_object
    source=create_engine('sqlite:///'+str(tmp_path/'backup-source.db'));Base.metadata.create_all(source);stamp(source)
    company=str(uuid.uuid4());id='doc_drill115';content=b'backup-and-restore-original'
    ref=write_object(content,'image/png',company,id,'original')
    with source.begin() as conn:
        conn.execute(Company.__table__.insert().values(id=company,name='DR test',api_key='restore-test'))
        conn.execute(Document.__table__.insert().values(company_id=company,api_id=id,filename='source.png',status='pushed',
            workflow_state={'id':id,'company_id':'restore-test','source_storage':ref,'status':'pushed','qbo_push_state':'succeeded','qbo_transaction_id':'existing-167'}))
    path=tmp_path/'postgres.rqbackup'
    try:
        backups.create(source,path);assert backups.verify(path)['verified']
        monkeypatch.setattr(settings,'local_storage_path',str(tmp_path/'restored'))
        result=backups.restore(pg,path,apply=True)
        assert result['outbound_hold'] and restore_guard.status()['active']
        with pg.connect() as conn:assert conn.scalar(select(Document.workflow_state))['qbo_transaction_id']=='existing-167'
        assert (Path(settings.local_storage_path)/ref['key']).read_bytes()==content
        restore_guard.release(result['restore_id']);assert not restore_guard.status()['active']
    finally:source.dispose()
