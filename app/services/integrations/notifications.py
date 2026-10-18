"""Transactional outbox. Provider acceptance is not proof of delivery."""
import html
import smtplib
import ssl
import uuid
from email.message import EmailMessage

import httpx
from pydantic import TypeAdapter, EmailStr, ValidationError
from sqlalchemy import inspect, select, update

from app.core import database
from app.core.config import settings
from app.models.database import NotificationOutbox, NotificationPreference, User, utc_now

NOTICES = {
    'extraction.succeeded': ('Belge çıkarımı tamamlandı', 'Belgenin çıkarılan verileri incelemeye hazır.'),
    'extraction.failed': ('Belge çıkarımı başarısız', 'Belgeyi uygulamada inceleyin; yeniden denemeden önce hata durumunu kontrol edin.'),
    'qbo.transaction.succeeded': ('QBO işlem kaydı oluştu', 'Finansal işlem kaydı doğrulandı. Kaynak ekinin durumunu ayrıca uygulamada kontrol edin.'),
    'qbo.transaction.uncertain': ('QBO sonucu inceleme bekliyor', 'Yazım sonucu belirsiz. Yeni işlem oluşturmadan önce uygulamadaki toparlanma akışını kullanın.'),
    'attachment.uncertain': ('QBO ek sonucu inceleme bekliyor', 'Kaynak veya çek eki gönderiminin sonucu belirsiz. Yeni ek göndermeden önce mevcut QBO eklerini uygulamada doğrulayın.'),
    'category.uncertain': ('QBO kategori sonucu inceleme bekliyor', 'Kategori yazımının sonucu belirsiz. Aynı yazımı tekrarlamadan önce uygulamadaki toparlanma akışını kullanın.'),
    'reconciliation.completed': ('Ekstre/çek işlemesi tamamlandı', 'Belgenin onaylanan eşleştirme ve kategori işlemleri tamamlandı.'),
}


def queue_transition(session, company_id, document_id, event_key, kind, state):
    if not settings.email_enabled or kind not in NOTICES:
        return
    if not inspect(session.connection()).has_table('notification_outbox'):
        return
    # Opt-in is per current account address, never a caller-supplied To.
    rows = session.execute(select(NotificationPreference, User).join(User,
        User.id == NotificationPreference.user_id).where(NotificationPreference.company_id == company_id,
        NotificationPreference.enabled.is_(True), User.company_id == company_id, User.is_active.is_(True))).all()
    for pref, user in rows:
        if pref.recipient_email != user.email:
            continue
        if session.scalar(select(NotificationOutbox.id).where(NotificationOutbox.user_id == user.id,
                NotificationOutbox.event_key == event_key)):
            continue
        session.add(NotificationOutbox(company_id=company_id, user_id=user.id,
            recipient_email=user.email, event_key=event_key, kind=kind, document_id=document_id,
            filename=str(state.get('filename') or document_id)[:255]))


def address(value):
    if not value or '\r' in value or '\n' in value:
        raise ValueError('Invalid email configuration')
    try:
        return str(TypeAdapter(EmailStr).validate_python(value))
    except ValidationError:
        raise ValueError('Invalid email configuration') from None


def configuration(recipient):
    sender, recipient = address(settings.email_from), address(recipient)
    if not settings.email_enabled:
        raise ValueError('Notifications disabled')
    if settings.email_provider == 'smtp':
        if not settings.smtp_host or any(c in settings.smtp_host for c in '\r\n'):
            raise ValueError('SMTP host is required')
        if bool(settings.smtp_username) != bool(settings.smtp_password):
            raise ValueError('SMTP credentials are incomplete')
    elif not getattr(settings, settings.email_provider + '_api_key'):
        raise ValueError('Provider credential is required')
    return sender, recipient


def send(row, configured):
    sender, recipient = configured
    subject, text = NOTICES[row.kind]
    text += '\n\nBelge: ' + row.filename + '\nKimlik: ' + row.document_id
    markup = '<p>' + html.escape(text).replace('\n', '<br>') + '</p>'
    if settings.email_provider == 'smtp':
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = sender, recipient, subject
        message['Message-ID'] = '<' + row.id + '@' + sender.rsplit('@', 1)[1] + '>'
        message.set_content(text)
        message.add_alternative(markup, subtype='html')
        context = ssl.create_default_context()
        smtp = (smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=context)
                if settings.smtp_security == 'ssl' else smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15))
        with smtp:
            if settings.smtp_security == 'starttls':
                smtp.starttls(context=context)
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            if smtp.send_message(message):
                raise RuntimeError('Provider did not accept the recipient')
        return str(message['Message-ID'])
    provider = settings.email_provider
    if provider == 'sendgrid':
        url = 'https://api.sendgrid.com/v3/mail/send'
        headers = {'Authorization': 'Bearer ' + settings.sendgrid_api_key}
        payload = {'personalizations': [{'to': [{'email': recipient}]}], 'from': {'email': sender},
                   'subject': subject, 'content': [{'type': 'text/plain', 'value': text}, {'type': 'text/html', 'value': markup}]}
    elif provider == 'resend':
        url = 'https://api.resend.com/emails'
        headers = {'Authorization': 'Bearer ' + settings.resend_api_key, 'Idempotency-Key': row.id}
        payload = {'from': sender, 'to': [recipient], 'subject': subject, 'text': text, 'html': markup}
    else:
        url = 'https://api.postmarkapp.com/email'
        headers = {'X-Postmark-Server-Token': settings.postmark_api_key}
        payload = {'From': sender, 'To': recipient, 'Subject': subject, 'TextBody': text, 'HtmlBody': markup}
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        result = client.post(url, headers=headers, json=payload)
        result.raise_for_status()
        if provider == 'sendgrid':
            if result.status_code != 202:
                raise RuntimeError('Unexpected provider result')
            return result.headers.get('X-Message-Id', '')[:255]
        body = result.json()
        if provider == 'postmark' and body.get('ErrorCode') != 0:
            raise RuntimeError('Provider rejected message')
        id = body.get('id' if provider == 'resend' else 'MessageID')
        if not isinstance(id, str) or not id:
            raise RuntimeError('Provider acceptance could not be verified')
        return id[:255]


def allowed(session, row):
    pref, user = session.get(NotificationPreference, row.user_id), session.get(User, row.user_id)
    return bool(pref and pref.enabled and pref.company_id == row.company_id and user and user.is_active
                and user.company_id == row.company_id and pref.recipient_email == row.recipient_email == user.email)


def run_once():
    if not settings.email_enabled:
        return None
    with database.SessionLocal() as session:
        row = session.scalar(select(NotificationOutbox).where(NotificationOutbox.status == 'pending')
                             .order_by(NotificationOutbox.created_at, NotificationOutbox.id).limit(1))
        if row is None:
            return None
        if not allowed(session, row):
            session.execute(update(NotificationOutbox).where(NotificationOutbox.id == row.id,
                NotificationOutbox.status == 'pending').values(status='cancelled', finished_at=utc_now()))
            session.commit()
            return row.id
        try:
            configured = configuration(row.recipient_email)
        except ValueError:
            session.execute(update(NotificationOutbox).where(NotificationOutbox.id == row.id,
                NotificationOutbox.status == 'pending').values(status='failed_before_send', finished_at=utc_now()))
            session.commit()
            return row.id
        token = uuid.uuid4().hex
        changed = session.execute(update(NotificationOutbox).where(NotificationOutbox.id == row.id,
            NotificationOutbox.status == 'pending').values(status='sending', claim_token=token, attempted_at=utc_now()))
        session.commit()  # Durable uncertainty boundary BEFORE contacting a provider.
        if changed.rowcount != 1:
            return None
        id = row.id
        try:
            provider_id, status = send(row, configured), 'accepted'
        except Exception:
            provider_id, status = None, 'uncertain'
        session.execute(update(NotificationOutbox).where(NotificationOutbox.id == id,
            NotificationOutbox.status == 'sending', NotificationOutbox.claim_token == token).values(
            status=status, provider_id=provider_id, finished_at=utc_now(), claim_token=None))
        session.commit()
        return id


def recover_notification(id):
    with database.SessionLocal() as session:
        changed = session.execute(update(NotificationOutbox).where(NotificationOutbox.id == id,
            NotificationOutbox.status == 'sending').values(status='uncertain', claim_token=None, finished_at=utc_now()))
        if changed.rowcount != 1:
            raise ValueError('Only an interrupted sending notification can be recovered')
        session.commit()
        return {'id': id, 'status': 'uncertain'}
