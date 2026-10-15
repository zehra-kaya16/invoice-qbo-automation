from app.services.persistence.check_sources import check_image_bytes, check_image_file, check_image_filename, CheckSourceError
"""Recover verified QBO results without reopening transaction creation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import tempfile
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal, Optional

from app.api import documents as api
from app.schemas.documents import DocumentType, ProcessingStatus

router = APIRouter()


class RecoveryRequest(BaseModel):
    key: str
    operation: Literal['inspect', 'verify', 'retry']
    expected_revision: int = Field(ge=1)
    expected_realm_id: str = Field(min_length=1)
    transaction_id: Optional[str] = None
    attachment_id: Optional[str] = None
    confirmed_attachment: bool = False
    acknowledged: bool = False


def realm_for(document):
    return document.get('qbo_match_realm_id') if document.get('document_type') == DocumentType.BANK_STATEMENT else (document.get('qbo_push_context') or {}).get('realm_id')


def _active(status, started):
    if status not in {'in_progress', 'pushing', 'uploading'}:
        return False
    try:
        value = datetime.fromisoformat(started)
        if value.tzinfo is None:
            return True
        return value > datetime.now(timezone.utc) - timedelta(minutes=5)
    except (ValueError, TypeError):
        return True


def _items(document):
    items = []
    if document.get('document_type') in {DocumentType.INVOICE, DocumentType.RECEIPT}:
        if document.get('qbo_push_state') in {'in_progress', 'uncertain'}:
            items.append({'key':'transaction', 'label':'Bill/Expense oluşturma sonucu', 'status':document['qbo_push_state'],
                          'transaction_id':document.get('qbo_transaction_id'), 'error':document.get('qbo_push_error'), 'can_retry':False})
        if document.get('qbo_transaction_id'):
            status = document.get('qbo_attachment_push_status')
            items.append({'key':'source', 'label':'Kaynak dosya eki', 'status':status or 'not_started',
                          'transaction_id':document['qbo_transaction_id'], 'attachment_id':document.get('qbo_attachment_id'),
                          'error':document.get('qbo_attachment_error'), 'can_retry':status in {None,'failed_before_write'} and not document.get('qbo_attachment_id')})
    elif document.get('document_type') == DocumentType.BANK_STATEMENT:
        categories = document.get('category_suggestions') or []
        checks = document.get('check_match_results') or []
        attempted = any(i.get('qbo_category_push_status') in {'pushing','pushed','uncertain'} for i in categories) or any(i.get('attachment_id') or i.get('attachment_error') or i.get('attachment_push_status') for i in checks)
        if attempted or document.get('status') == ProcessingStatus.PUSHED:
            for item in categories:
                if item.get('review_status') not in {'approved','manual_override'}:
                    continue
                status = item.get('qbo_category_push_status', 'not_pushed')
                items.append({'key':f"category:{item['transaction_index']}", 'label':f"İşlem {item['transaction_index']+1} kategori yazımı",
                              'status':status, 'transaction_id':item.get('qbo_transaction_id'), 'desired_account':(item.get('reviewed_category') or {}).get('id'),
                              'error':item.get('qbo_category_push_error'), 'can_retry':status in {'uncertain','not_pushed','pushing'} and not _active(status,item.get('qbo_category_started_at'))})
            for item in checks:
                if not item.get('matched'):
                    continue
                status = item.get('attachment_push_status')
                items.append({'key':f"check:{item['check_index']}", 'label':f"Çek {item['check_index']+1} görsel eki",
                              'status':status or 'not_started', 'transaction_id':item.get('qbo_transaction_id'), 'attachment_id':item.get('attachment_id'),
                              'error':item.get('attachment_error'), 'can_retry':status is None and not item.get('attachment_id')})
    return items


def snapshot(document):
    realm = realm_for(document)
    return {'id':document['id'], 'revision':document['_revision'], 'realm_id':realm,
            'items':_items(document), 'history':document.get('qbo_recovery_history', [])[-10:],
            'blocked_reason':None if realm else 'Bu eski kayıtta yazımın QBO şirketi saklanmamış; güvenli toparlanma açılamıyor.'}


@router.get('/{document_id}/recovery')
def get_recovery(document_id: str):
    return snapshot(api._load_document(document_id))


def _entry(document, key):
    if key == 'transaction' and document.get('document_type') in {DocumentType.INVOICE, DocumentType.RECEIPT}:
        return document
    if key == 'source' and document.get('qbo_transaction_id'):
        return document
    for prefix, field, index in [('category:', 'category_suggestions','transaction_index'), ('check:', 'check_match_results','check_index')]:
        if key.startswith(prefix):
            try:
                number = int(key[len(prefix):])
            except ValueError:
                break
            entry = next((item for item in document.get(field, []) if item.get(index) == number), None)
            if entry is not None:
                return entry
    raise HTTPException(404, 'Recovery item was not found')


def _read(operation, *args):
    try:
        return operation(*args)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'QBO read failed; the local write lock is preserved') from exc


def _money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise HTTPException(409, 'QBO amount could not be verified')


def _invoice_identity(document, transaction, client):
    context = document.get('qbo_push_context') or {}
    plan = context.get('plan') or {}
    args = plan.get('kwargs') or {}
    create_as = 'bill' if plan.get('method') == 'create_bill' else 'expense'
    if not context.get('marker') or transaction.get('marker') != context['marker']:
        raise HTTPException(409, 'The QBO transaction does not carry this write attempt identity')
    currency = transaction.get('currency')
    if not currency:
        currency = _read(client.get_currency_preferences).get('home_currency')
    correct = transaction.get('type') == create_as and transaction.get('date') == args.get('date')
    correct = correct and str(transaction.get('vendor_id')) == str(args.get('vendor_id'))
    correct = correct and _money(transaction.get('amount')) == _money(args.get('amount'))
    correct = correct and transaction.get('expense_accounts') == [str(args.get('account_id'))]
    correct = correct and currency == context.get('currency')
    if create_as == 'expense':
        correct = correct and str(transaction.get('bank_account_id')) == str(args.get('bank_account_id'))
    if not correct:
        raise HTTPException(409, 'The QBO transaction differs from the original write plan')


def _bank_identity(document, entry, transaction):
    index = entry.get('transaction_index', entry.get('statement_transaction_index'))
    values = (document.get('extracted_data') or {}).get('transactions', [])
    if type(index) is not int or index < 0 or index >= len(values):
        raise HTTPException(409, 'Original bank transaction was not found')
    original = values[index]
    if transaction.get('date') != original.get('date') or abs(_money(transaction.get('amount'))) != abs(_money(original.get('amount'))):
        raise HTTPException(409, 'The QBO transaction differs from the original bank transaction')


def _attachment_info(document, entry, key):
    if key == 'source':
        kind = 'Bill' if document.get('qbo_transaction_type') == 'bill' else 'Purchase'
        if document.get('qbo_attachment_source') is not None:
            from app.services.persistence.attachment_sources import invoice_attachment
            filename, content_type, content = invoice_attachment(document)
            return kind, document['qbo_transaction_id'], document.get('qbo_attachment_marker'), filename, len(content)
        content = document.get('content')
        return kind, document['qbo_transaction_id'], document.get('qbo_attachment_marker'), document['filename'], len(content) if isinstance(content,bytes) else None
    path = Path(entry.get('image_path') or '')
    kind = api._qbo_attachment_entity_type(entry.get('qbo_transaction_type'))
    if not kind or not entry.get('qbo_transaction_id'):
        raise HTTPException(409, 'Check QBO target was not recorded')
    try:
        size = len(check_image_bytes(document, entry.get('image_path')))
    except CheckSourceError:
        size = None
    return kind, entry['qbo_transaction_id'], entry.get('attachment_marker'), check_image_filename(document, entry.get('image_path')), size


def _mark_attachment(document, entry, key, attachment_id):
    if key == 'source':
        document.update(qbo_attachment_id=str(attachment_id), qbo_attachment_push_status='uploaded', qbo_attachment_error=None)
    else:
        entry.update(attachment_id=str(attachment_id), attachment_push_status='uploaded', attachment_error=None)


def _finish_bank(document):
    if document.get('document_type') != DocumentType.BANK_STATEMENT:
        return
    if document.get('category_suggestions') is None or document.get('check_match_results') is None:
        return
    try:
        api._prepare_reviewed_bank_statement_matches(document)
        api._validate_bank_statement_vendors_for_push(document)
        api._validate_bank_statement_check_results(document)
        api._prepare_reviewed_bank_statement_categories(document)
    except HTTPException:
        return
    categories_done = all(i.get('review_status') == 'rejected' or i.get('qbo_category_push_status') == 'pushed' for i in document['category_suggestions'])
    checks_done = all(not i.get('matched') or i.get('attachment_id') for i in document['check_match_results'])
    if categories_done and checks_done:
        document['status'] = ProcessingStatus.PUSHED


def _audit(document, key, operation, previous_error=None):
    values = document.setdefault('qbo_recovery_history', [])
    values.append({'key':key, 'operation':operation, 'time':datetime.now(timezone.utc).isoformat(), 'previous_error':previous_error})
    document['qbo_recovery_history'] = values[-100:]


@router.post('/{document_id}/recovery')
def recover(document_id: str, request: RecoveryRequest):
    document = api._load_document(document_id)
    if request.expected_revision != document['_revision']:
        raise HTTPException(409, 'Recovery state changed; reload before continuing')
    realm = realm_for(document)
    if not realm or realm != request.expected_realm_id:
        raise HTTPException(409, 'The original QBO company was not verified')
    from app.api.qbo import _get_connected_qbo_client
    client = _get_connected_qbo_client()
    if getattr(client, 'realm_id', None) != realm:
        raise HTTPException(409, 'Connect the original QBO company before recovery')
    entry = _entry(document, request.key)
    available = next((item for item in _items(document) if item['key'] == request.key), None)
    if available is None:
        raise HTTPException(409, 'This item is not eligible for recovery')
    provider = None
    if request.key == 'transaction':
        context = document.get('qbo_push_context') or {}
        plan = context.get('plan') or {}
        create_as = 'bill' if plan.get('method') == 'create_bill' else 'expense'
        if not context.get('marker') or not plan.get('kwargs'):
            raise HTTPException(409, 'The original write plan/identity was not recorded')
        found = [_read(client.get_recovery_transaction, create_as, request.transaction_id)] if request.transaction_id else _read(client.find_recovery_transactions, create_as, plan['kwargs']['date'], context['marker'])
        provider = {'transactions':found}
        if request.operation == 'retry':
            raise HTTPException(409, 'Recovery never creates another Bill/Expense')
        if request.operation == 'verify':
            if len(found) != 1:
                raise HTTPException(409, 'Exactly one matching QBO transaction is required; the lock is preserved')
            _invoice_identity(document, found[0], client)
            document.update(qbo_transaction_id=str(found[0]['id']), qbo_transaction_type=create_as,
                            qbo_push_state='succeeded', qbo_push_error=None, status=ProcessingStatus.PUSHED)
    elif request.key.startswith('category:'):
        if entry.get('review_status') not in {'approved','manual_override'} or not entry.get('reviewed_category'):
            raise HTTPException(409, 'No approved category was recorded')
        transaction = _read(client.get_recovery_transaction, 'purchase', str(entry['qbo_transaction_id']))
        _bank_identity(document, entry, transaction)
        desired = str(entry['reviewed_category']['id'])
        actual = (transaction.get('expense_accounts') or [None])[0]
        provider = {'transaction':transaction, 'desired_account':desired, 'current_account':actual}
        if request.operation != 'inspect':
            if actual != desired:
                if request.operation != 'retry' or not request.acknowledged or not available['can_retry']:
                    raise HTTPException(409, 'QBO category does not match; inspect and explicitly retry the approved category')
                entry.update(qbo_category_push_status='pushing', qbo_category_account_id=desired,
                             qbo_category_started_at=datetime.now(timezone.utc).isoformat())
                api._save_document(document)
                try:
                    result = client.update_purchase_category(purchase_id=str(entry['qbo_transaction_id']),account_id=desired)
                    if str(result.get('id')) != str(entry['qbo_transaction_id']) or str(result.get('account_id')) != desired:
                        raise ValueError('QBO update returned an inconsistent result')
                except Exception as exc:
                    entry.update(qbo_category_push_status='uncertain',qbo_category_push_error=str(exc))
                    api._save_document(document)
                    raise HTTPException(502, 'Category update outcome is uncertain; inspect QBO again') from exc
            entry.update(qbo_category_push_status='pushed',qbo_category_account_id=desired,qbo_category_push_error=None,
                         qbo_category_push_result={'id':str(entry['qbo_transaction_id']),'account_id':desired,'recovered':True})
    else:
        if request.key == 'source' and document.get('qbo_push_state') != 'succeeded':
            raise HTTPException(409, 'Verify the QBO transaction before recovering its source attachment')
        kind, target_id, marker, filename, size = _attachment_info(document, entry, request.key)
        transaction_type = document['qbo_transaction_type'] if request.key == 'source' else entry['qbo_transaction_type']
        transaction = _read(client.get_recovery_transaction, transaction_type, str(target_id))
        if request.key == 'source':
            _invoice_identity(document, transaction, client)
        else:
            _bank_identity(document, entry, transaction)
        attachments = _read(client.get_recovery_attachments, kind, str(target_id))
        provider = {'attachments':attachments,'file_name':filename,'size':size,'transaction_id':str(target_id)}
        chosen = [item for item in attachments if marker and item.get('marker') == marker]
        if request.attachment_id:
            chosen = [item for item in attachments if str(item.get('id')) == request.attachment_id]
        if request.operation != 'inspect':
            if len(chosen) > 1:
                raise HTTPException(409, 'Several attachment matches were found; inspect QBO before choosing an ID')
            if chosen:
                value = chosen[0]
                if value.get('marker') != marker or not marker:
                    if not request.attachment_id or not request.confirmed_attachment or size is None or value.get('file_name') != filename or value.get('size') != size:
                        raise HTTPException(409, 'Confirm the selected attachment content; filename and size must match')
                _mark_attachment(document,entry,request.key,value['id'])
            elif request.operation == 'verify':
                raise HTTPException(409, 'No verified attachment found; the uncertain upload is not repeated')
            else:
                if request.attachment_id:
                    raise HTTPException(409, 'Selected attachment was not found on this QBO transaction')
                if not request.acknowledged or not available['can_retry']:
                    raise HTTPException(409, 'An uncertain/active attachment upload cannot be repeated; use QBO inspection')
                # Only a known pre-write failure/not-started upload may be sent.
                marker = marker or 'receipt-ai:' + uuid.uuid4().hex + ':attachment'
                temp_path = None
                try:
                    if request.key == 'source':
                        from app.services.persistence.attachment_sources import invoice_attachment
                        filename, content_type, content = invoice_attachment(document)
                        with tempfile.NamedTemporaryFile(delete=False,suffix=Path(filename).suffix) as temp:
                            temp.write(content);temp_path=temp.name
                        path=temp_path
                        document.update(qbo_attachment_marker=marker,qbo_attachment_push_status='uploading',qbo_attachment_started_at=datetime.now(timezone.utc).isoformat())
                    else:
                        content_type='image/png'
                        try:
                            content=check_image_bytes(document, entry.get('image_path'))
                        except CheckSourceError as exc:
                            raise HTTPException(409,str(exc)) from None
                        with tempfile.NamedTemporaryFile(delete=False,suffix='.png') as temp:
                            temp.write(content);temp_path=temp.name
                        path=temp_path
                        # Category writes must finish before check attachments.
                        if any(i.get('review_status')!='rejected' and i.get('qbo_category_push_status')!='pushed' for i in document.get('category_suggestions',[])):
                            raise HTTPException(409,'Recover every approved category before uploading check images')
                        entry.update(attachment_marker=marker,attachment_push_status='uploading',attachment_started_at=datetime.now(timezone.utc).isoformat())
                    api._save_document(document)
                    try:
                        result=client.upload_attachment(file_path=path,file_name=filename,content_type=content_type,entity_type=kind,entity_id=str(target_id),recovery_marker=marker)
                        if not result.get('id'):raise ValueError('QBO attachment returned no ID')
                    except Exception as exc:
                        if request.key=='source':document.update(qbo_attachment_push_status='uncertain',qbo_attachment_error=str(exc))
                        else:entry.update(attachment_push_status='uncertain',attachment_error=str(exc))
                        api._save_document(document)
                        raise HTTPException(502,'Attachment outcome is uncertain; inspect QBO instead of repeating the upload') from exc
                    _mark_attachment(document,entry,request.key,result['id'])
                finally:
                    if temp_path and os.path.exists(temp_path):os.remove(temp_path)
    if request.operation != 'inspect':
        _finish_bank(document)
        _audit(document,request.key,request.operation,available.get("error"))
        api._save_document(document)
    return {'recovery':snapshot(document),'provider':provider}
