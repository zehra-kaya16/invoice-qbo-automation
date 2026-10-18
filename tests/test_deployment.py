from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.config import settings
from app.models.database import Base, Document, Company, User, WebhookIntegration
from app.services import deployment
from app.services.deployment.transfer import transfer, TransferError
from app.services.persistence.repository import DocumentRepository
from prepare_docker import prepare

ROOT=Path(__file__).resolve().parents[1]


def stamp(engine):
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)'))
        conn.execute(text('INSERT INTO alembic_version VALUES (:v)'),{'v':deployment.expected_head()})


@pytest.fixture
def pair(isolated_document_database,monkeypatch,tmp_path):
    source=isolated_document_database.kw['bind'];stamp(source)
    target=create_engine('sqlite:///'+str(tmp_path/'target.db'))
    Base.metadata.create_all(target);stamp(target)
    monkeypatch.setattr(settings,'email_enabled',False)
    DocumentRepository().save({'id':'doc_move','filename':'original.png','company_id':'default',
        'content_type':'image/png','content':b'source-byte-content','status':'pushed','approved':True,
        'qbo_transaction_id':'167','qbo_push_state':'succeeded','extracted_data':{'currency':'USD','total':19.09,'invoice_number':'INV-109'}})
    original=Path(settings.local_storage_path)
    monkeypatch.setattr(settings,'local_storage_path',str(tmp_path/'target-objects'))
    try:yield source,target,original
    finally:target.dispose()


def test_raw_env_preserves_secrets_and_original(tmp_path,monkeypatch):
    source=tmp_path/'source.env';key=Fernet.generate_key().decode()
    raw='QBO_TOKEN_ENCRYPTION_KEY='+key+'''\nSMTP_PASSWORD='literal$with#hash and "quotes"'\nEMAIL_ENABLED=true\nDATABASE_URL=sqlite:///old.db\nLOCAL_STORAGE_ID=original-store\n'''
    monkeypatch.setenv('PREPARE_SMTP_USER','resolved-user')
    raw+='SMTP_USERNAME=${PREPARE_SMTP_USER}\n'
    source.write_text(raw);prepare(source,tmp_path,'0.0.0.0')
    assert source.read_text()==raw
    runtime=(tmp_path/'.env.docker').read_text();infra=(tmp_path/'.env.compose').read_text()
    assert 'SMTP_PASSWORD=literal$with#hash and "quotes"\n' in runtime
    assert 'SMTP_USERNAME=resolved-user\n' in runtime
    assert 'EMAIL_ENABLED=false\n' in runtime and 'LOCAL_STORAGE_ID=original-store\n' in runtime
    assert key in runtime and 'DATABASE_URL=' not in runtime
    assert 'APP_BIND=0.0.0.0' in infra and key not in infra
    password=next(x.split('=',1)[1] for x in infra.splitlines() if x.startswith('POSTGRES_PASSWORD='))
    assert len(password)==64
    with pytest.raises(ValueError):prepare(source,tmp_path)
    assert (tmp_path/'.env.compose').read_text()==infra


@pytest.mark.parametrize('invalid',['missing-key','bad-key','multiline','missing-file'])
def test_prepare_rejects_bad_configuration(tmp_path,invalid):
    source=tmp_path/'source.env'
    if invalid!='missing-file':
        source.write_text(('QBO_TOKEN_ENCRYPTION_KEY='+Fernet.generate_key().decode()+'\n') if invalid=='multiline'
            else 'QBO_TOKEN_ENCRYPTION_KEY=bad\n' if invalid=='bad-key' else '')
        if invalid=='multiline':source.write_text(source.read_text()+"SMTP_PASSWORD='line1\nline2'\n")
    with pytest.raises(ValueError):prepare(source,tmp_path)
    assert not (tmp_path/'.env.compose').exists() and not (tmp_path/'.env.docker').exists()


def test_compose_migration_dependencies_storage_and_private_ports():
    services=yaml.safe_load((ROOT/'docker-compose.yml').read_text())['services']
    assert services['app']['depends_on']['migrate']['condition']=='service_completed_successfully'
    assert services['migrate']['restart']=='no'
    assert 'profiles' not in services['worker'] and 'profiles' not in services['beat']
    assert services['worker']['depends_on']['app']['condition']=='service_healthy'
    assert services['beat']['depends_on']['worker']['condition']=='service_healthy'
    assert '--pidfile=' in services['beat']['command']
    for name in ['app','migrate','worker','beat','operator']:
        s=services[name]
        assert 'upload_data:/app/uploads' in s['volumes']
        assert s['env_file']==[{'path':'.env.docker','format':'raw'}]
        assert s['environment']['DEPLOYMENT_MODE']=='compose'
        assert not any('./app:' in v for v in s['volumes'])
    assert 'ports' not in services['db'] and 'ports' not in services['redis']
    assert '--requirepass' in services['redis']['command']
    assert 'alembic upgrade head' not in (ROOT/'docker-entrypoint.sh').read_text()
    ignore=(ROOT/'.dockerignore').read_text()
    assert '.env.*' in ignore and '*.db' in ignore and 'uploads' in ignore


@pytest.mark.parametrize('bad',['sqlite','db-localhost','redis-localhost','redis-no-auth','key','path'])
def test_compose_runtime_fails_closed(monkeypatch,bad):
    monkeypatch.setattr(settings,'deployment_mode','compose')
    monkeypatch.setattr(settings,'database_url','postgresql://u:p@db/test')
    monkeypatch.setattr(settings,'redis_url','redis://:p@redis:6379/0')
    monkeypatch.setattr(settings,'local_storage_path',str(Path('uploads').resolve()))
    monkeypatch.setattr(settings,'qbo_token_encryption_key',Fernet.generate_key().decode())
    if bad=='sqlite':monkeypatch.setattr(settings,'database_url','sqlite:///a.db')
    elif bad=='db-localhost':monkeypatch.setattr(settings,'database_url','postgresql://u:p@localhost/test')
    elif bad=='redis-localhost':monkeypatch.setattr(settings,'redis_url','redis://:p@localhost/0')
    elif bad=='redis-no-auth':monkeypatch.setattr(settings,'redis_url','redis://redis/0')
    elif bad=='key':monkeypatch.setattr(settings,'qbo_token_encryption_key','bad')
    else:monkeypatch.setattr(settings,'local_storage_path','./uploads')
    with pytest.raises(ValueError):deployment.validate_runtime()


def test_valid_runtime_and_local_default(monkeypatch):
    monkeypatch.setattr(settings,'deployment_mode','compose')
    monkeypatch.setattr(settings,'database_url','postgresql://u:p@db/test')
    monkeypatch.setattr(settings,'redis_url','redis://:p@redis:6379/0')
    monkeypatch.setattr(settings,'local_storage_path',str(Path('uploads').resolve()))
    monkeypatch.setattr(settings,'qbo_token_encryption_key',Fernet.generate_key().decode())
    deployment.validate_runtime()
    monkeypatch.setattr(settings,'deployment_mode','local')
    monkeypatch.setattr(settings,'database_url','sqlite:///a.db')
    deployment.validate_runtime()


@pytest.mark.parametrize('value,healthy',[(None,False),('bad',False),('0',False),('990',True),('960',True),('950',False),('1001',False)])
def test_beat_health_freshness(value,healthy):
    assert deployment.beat_healthy(SimpleNamespace(get=lambda key:value),now=1000) is healthy


def test_scheduler_heartbeat_written_after_tick(monkeypatch):
    from celery.beat import PersistentScheduler
    from app.tasks.scheduler import DeploymentScheduler
    calls=[]
    monkeypatch.setattr(PersistentScheduler,'tick',lambda *a,**k:2)
    monkeypatch.setattr('app.tasks.scheduler.connection',lambda:SimpleNamespace(set=lambda *a,**k:calls.append((a,k))))
    assert object.__new__(DeploymentScheduler).tick()==2
    assert calls[0][0][0]==deployment.BEAT_KEY and calls[0][1]['ex']==45


def test_transfer_dryrun_preserves_source_then_copies_same_ids(pair):
    source,target,original=pair
    report=transfer(source,target,local_from=original,require_postgres=False)
    assert report['mode']=='dry-run' and report['rows']['documents']==1 and report['objects_verified']==1
    with target.connect() as c:assert c.scalar(select(func.count()).select_from(Document))==0
    report=transfer(source,target,local_from=original,apply=True,require_postgres=False)
    assert report['mode']=='applied' and report['objects_copied']==1
    with target.connect() as c:
        doc=c.execute(select(Document.__table__)).mappings().one();state=doc['workflow_state']
        assert doc['api_id']=='doc_move' and state['qbo_transaction_id']=='167' and state['approved'] is True
        assert state['extracted_data']['currency']=='USD'
        assert (Path(settings.local_storage_path)/state['source_storage']['key']).read_bytes()==b'source-byte-content'
    with source.connect() as c:assert c.scalar(select(Document.api_id))=='doc_move'
    with pytest.raises(TransferError,match='not empty'):transfer(source,target,local_from=original,apply=True,require_postgres=False)


@pytest.mark.parametrize('failure',['missing','corrupt','wrong-location','wrong-key','active','category-active','target-nonempty','head','extra-table','columns'])
def test_transfer_refuses_unsafe_source(pair,monkeypatch,failure):
    source,target,original=pair
    with source.begin() as c:
        state=dict(c.scalar(select(Document.workflow_state)))
        if failure in {'missing','corrupt'}:
            obj=original/state['source_storage']['key']
            if failure=='missing':obj.unlink()
            else:obj.write_bytes(b'wrong')
        elif failure=='wrong-location':monkeypatch.setattr(settings,'local_storage_id','other-store')
        elif failure=='wrong-key':
            company=c.scalar(select(Company.id));owner='a0000000-0000-0000-0000-000000000114'
            c.execute(User.__table__.insert().values(id=owner,company_id=company,email='transfer@example.test',role='admin',is_active=True))
            c.execute(WebhookIntegration.__table__.insert().values(company_id=company,owner_user_id=owner,name='move',secret=Fernet(Fernet.generate_key()).encrypt(b'secret').decode(),enabled=True,auto_extract=False))
            monkeypatch.setattr(settings,'qbo_token_encryption_key',Fernet.generate_key().decode())
        elif failure=='active':
            state['extraction_claim']='active';c.execute(Document.__table__.update().values(workflow_state=state))
        elif failure=='category-active':
            state['category_suggestions']=[{'transaction_index':0,'qbo_category_push_status':'pushing'}];c.execute(Document.__table__.update().values(workflow_state=state))
        elif failure=='head':c.execute(text("UPDATE alembic_version SET version_num='old'"))
        elif failure=='extra-table':c.execute(text('CREATE TABLE unknown_data (id INTEGER)'))
        elif failure=='columns':c.execute(text('ALTER TABLE companies ADD COLUMN unknown_data TEXT'))
    if failure=='target-nonempty':
        with target.begin() as c:c.execute(Company.__table__.insert().values(name='existing'))
    with pytest.raises(TransferError):transfer(source,target,local_from=original,apply=True,require_postgres=False)
    with target.connect() as c:assert c.scalar(select(func.count()).select_from(Document))==0


def test_transfer_transaction_rolls_back_on_content_mismatch(pair,monkeypatch):
    from app.services.deployment import transfer as module
    source,target,original=pair;real=module._digest
    def mismatch(conn,table):
        count,digest=real(conn,table)
        return (count,'mismatch') if conn.engine is target and table.name=='documents' else (count,digest)
    monkeypatch.setattr(module,'_digest',mismatch)
    with pytest.raises(TransferError,match='rolled back'):transfer(source,target,local_from=original,apply=True,require_postgres=False)
    with target.connect() as c:
        assert c.scalar(select(func.count()).select_from(Document))==0
        assert c.scalar(select(func.count()).select_from(Company))==0


def test_probe_nonce_and_shared_object(pair,monkeypatch):
    _,_,original=pair;nonce='a'*32
    monkeypatch.setattr(settings,'local_storage_path',str(original));monkeypatch.setattr(deployment,'schema_check',lambda:None)
    path=original/deployment.probe_key(nonce);path.parent.mkdir(parents=True);path.write_bytes(nonce.encode())
    assert deployment.probe(nonce,deployment.probe_key(nonce))['shared_storage']
    with pytest.raises(ValueError):deployment.probe(nonce,'../data')
    path.write_bytes(b'wrong')
    with pytest.raises(ValueError):deployment.probe(nonce,deployment.probe_key(nonce))


def test_postgresql_ddl_and_json_query_compilation():
    for table in Base.metadata.sorted_tables:assert 'CREATE TABLE' in str(CreateTable(table).compile(dialect=postgresql.dialect()))
    sql=str(select(Document.workflow_state['approved'].as_boolean()).compile(dialect=postgresql.dialect()))
    assert 'BOOLEAN' in sql and '->>' in sql


def test_snapshot_captures_committed_wal_and_never_overwrites(tmp_path):
    import sqlite3
    from manage_database_transfer import snapshot
    source=tmp_path/'wal.db';output=tmp_path/'snapshot.db'
    with sqlite3.connect(source) as live:
        live.execute('PRAGMA journal_mode=WAL');live.execute('CREATE TABLE events(id INTEGER)');live.execute('INSERT INTO events VALUES (114)');live.commit()
        result=snapshot(output,source)
        assert result['integrity']=='ok'
        with sqlite3.connect(output) as copy:assert copy.execute('SELECT id FROM events').fetchone()[0]==114
        with pytest.raises(FileExistsError):snapshot(output,source)
        assert live.execute('SELECT id FROM events').fetchone()[0]==114


def test_snapshot_missing_source_does_not_create_database(tmp_path):
    from manage_database_transfer import snapshot
    source=tmp_path/'missing.db';output=tmp_path/'snapshot.db'
    with pytest.raises(TransferError):snapshot(output,source)
    assert not output.exists() and not source.exists()


@pytest.mark.parametrize('failure',[None,'fingerprint','worker','beat','redis','sqlite'])
def test_stack_probe_rejects_mismatch_and_cleans_object(pair,monkeypatch,failure):
    from app.core import database
    import time
    _,_,_=pair;called=[]
    monkeypatch.setattr(deployment,'validate_runtime',lambda:None)
    monkeypatch.setattr(deployment,'schema_check',lambda:None)
    monkeypatch.setattr(database,'engine',SimpleNamespace(dialect=SimpleNamespace(name='sqlite' if failure=='sqlite' else 'postgresql')))
    monkeypatch.setattr(deployment,'connection',lambda:SimpleNamespace(ping=lambda:failure!='redis',
        get=lambda key:'0' if failure=='beat' else str(time.time())))
    class Result:
        def __init__(self,nonce,key):self.nonce=nonce;self.key=key
        def get(self,**kwargs):
            assert kwargs['timeout']==40
            reply=deployment.probe(self.nonce,self.key)
            if failure=='fingerprint':reply['fingerprint']='different'
            return reply
        def forget(self):called.append('forgotten')
    def send(name,args,**kwargs):
        assert name=='receipt_ai.deployment_probe' and kwargs['expires']==30
        called.append(args[1]);return Result(*args)
    celery=SimpleNamespace(control=SimpleNamespace(inspect=lambda **k:SimpleNamespace(ping=lambda:None if failure=='worker' else {'worker':{'ok':'pong'}})),send_task=send)
    if failure:
        with pytest.raises(ValueError):deployment.check_stack(celery)
    else:assert deployment.check_stack(celery)['shared_storage']=='ready'
    if failure in {None,'fingerprint'}:
        assert called[-1]=='forgotten' and not (Path(settings.local_storage_path)/called[0]).exists()


def test_schema_check_rejects_wrong_revision(pair,monkeypatch):
    from app.core import database
    source,_,_=pair;monkeypatch.setattr(database,'engine',source)
    deployment.schema_check()
    with source.begin() as c:c.execute(text("UPDATE alembic_version SET version_num='old'"))
    with pytest.raises(ValueError):deployment.schema_check()


def test_migration_url_with_percent_is_not_config_interpolation(tmp_path,monkeypatch):
    from alembic import command
    from alembic.config import Config
    path=tmp_path/'literal%percent.db'
    monkeypatch.setattr(settings,'database_url','sqlite:///'+str(path))
    config=Config(str(ROOT/'alembic.ini'));config.set_main_option('script_location',str(ROOT/'migrations'))
    command.upgrade(config,'head')
    assert path.exists()
