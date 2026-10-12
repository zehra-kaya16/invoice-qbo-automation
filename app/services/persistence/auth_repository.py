"""Persistent sessions, users and login throttling. No public registration."""
import secrets
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update

from app.core import database
from app.core.auth import Principal, hash_password, token_hash, verify_password
from app.core.config import settings
from app.models.database import AuthLoginAttempt, AuthSession, Company, User, utc_now


def company_uuid(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"invoice-qbo-automation:company:{key}"))


def normalize_email(email):
    email = email.strip().casefold()
    if len(email) > 255 or email.count("@") != 1 or not all(email.split("@")) or any(c.isspace() for c in email):
        raise ValueError("A valid email is required")
    return email


def user_summary(row):
    return {"id": row.id, "email": row.email, "name": row.name, "role": row.role, "is_active": row.is_active}


class AuthRepository:
    def session(self):
        return database.SessionLocal()

    def principal(self, session_hash):
        with self.session() as session:
            record = session.get(AuthSession, session_hash)
            if record is None or record.expires_at <= utc_now():
                return None
            user = session.get(User, record.user_id)
            if user is None or not user.is_active or user.role not in {"admin", "user"}:
                return None
            company = session.get(Company, user.company_id)
            if company is None or not company.api_key:
                return None
            return Principal(user.id, company.id, company.api_key, company.name, user.email,
                             user.name or "", user.role, record.token_hash, record.csrf_token)

    def login(self, email, password, peer):
        email = normalize_email(email)
        now = utc_now()
        subject = token_hash(email)
        peer_key = token_hash("peer:" + peer)
        with self.session() as session, session.begin():
            session.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.created_at < now - timedelta(minutes=15)))
            for key, limit in [(subject, 5), (peer_key, 30)]:
                count = session.scalar(select(func.count()).select_from(AuthLoginAttempt).where(AuthLoginAttempt.subject_hash == key))
                if count >= limit:
                    raise HTTPException(429, "Too many sign-in attempts; try again in 15 minutes")
            # Count every attempt before checking credentials, including successes.
            session.add_all([AuthLoginAttempt(subject_hash=subject, created_at=now),
                             AuthLoginAttempt(subject_hash=peer_key, created_at=now)])
        with self.session() as session, session.begin():
            user = session.scalar(select(User).where(User.email == email))
            # Use the same KDF for unknown users, never reveal account existence.
            dummy = "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32
            correct = verify_password(password, (user.password_hash or dummy) if user else dummy)
            if not correct or user is None or not user.is_active:
                raise HTTPException(401, "Email or password is incorrect")
            company = session.get(Company, user.company_id)
            if company is None or not company.api_key or user.role not in {"admin", "user"}:
                raise HTTPException(401, "Email or password is incorrect")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
            session.add(AuthSession(token_hash=token_hash(token), user_id=user.id, csrf_token=csrf,
                                    expires_at=now + timedelta(hours=max(1, min(settings.auth_session_hours, 24)))))
            user.last_login = now
            session.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.subject_hash == subject))
        return token

    def logout(self, user):
        with self.session() as session, session.begin():
            session.execute(delete(AuthSession).where(AuthSession.token_hash == user.session_hash))

    def users(self, company_id):
        with self.session() as session:
            return [user_summary(row) for row in session.scalars(select(User).where(User.company_id == company_id).order_by(User.email))]

    def create_user(self, company_id, email, password, name, role="user"):
        email = normalize_email(email)
        password_hash = hash_password(password)
        with self.session() as session, session.begin():
            if session.scalar(select(User).where(User.email == email)):
                raise HTTPException(409, "This email cannot be used")
            if session.get(Company, company_id) is None:
                raise ValueError("Company must be initialized first")
            row = User(company_id=company_id, email=email, password_hash=password_hash,
                       name=name, role=role, is_active=True)
            session.add(row)
            session.flush()
            return user_summary(row)

    def change_user(self, actor, id, is_active, role):
        with self.session() as session, session.begin():
            # Serialize role changes on SQLite and PostgreSQL; retain an active admin.
            session.execute(update(Company).where(Company.id == actor.company_id).values(updated_at=utc_now()))
            current_actor = session.get(User, actor.user_id)
            if current_actor is None or not current_actor.is_active or current_actor.role != "admin":
                raise HTTPException(403, "Administrator permission changed; sign in again")
            row = session.scalar(select(User).where(User.id == id, User.company_id == actor.company_id))
            if row is None:
                raise HTTPException(404, "User was not found")
            if row.id == actor.user_id:
                raise HTTPException(409, "Your own account cannot be disabled or demoted here")
            if row.role == "admin" and row.is_active and (is_active is False or role == "user"):
                count = session.scalar(select(func.count()).select_from(User).where(User.company_id == actor.company_id, User.role == "admin", User.is_active.is_(True)))
                if count <= 1:
                    raise HTTPException(409, "The company must retain an active administrator")
            if is_active is not None:
                row.is_active = is_active
            if role is not None:
                row.role = role
            # Existing sessions never keep their old role/active status.
            session.execute(delete(AuthSession).where(AuthSession.user_id == row.id))
            session.flush()
            return user_summary(row)

    def change_password(self, actor, old_password, password):
        new_hash = hash_password(password)
        with self.session() as session, session.begin():
            row = session.get(User, actor.user_id)
            if row is None or not verify_password(old_password, row.password_hash):
                raise HTTPException(403, "Current password is incorrect")
            row.password_hash = new_hash
            session.execute(delete(AuthSession).where(AuthSession.user_id == row.id))
