"""Trusted local operator: initialize companies and recover user passwords."""
import argparse
import getpass
import re

from sqlalchemy import delete, select
from app.core import database
from app.core.auth import hash_password
from app.models.database import AuthSession, Company, User
from app.services.persistence.auth_repository import AuthRepository, company_uuid, normalize_email


def initialize_company(key, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", key):
        raise ValueError("Company key must use lowercase letters, digits, - or _")
    with database.SessionLocal() as session, session.begin():
        id = company_uuid(key)
        row = session.get(Company, id)
        if row is None:
            session.add(Company(id=id, name=name, api_key=key))
        elif row.api_key not in {None, key}:
            raise ValueError("Company key conflicts with an existing company")
        else:
            row.api_key = key
            row.name = name
    return id


def read_password():
    first = getpass.getpass("New password (12–128 characters): ")
    if first != getpass.getpass("Repeat password: "):
        raise ValueError("Passwords do not match")
    return first


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--company", required=True)
    create.add_argument("--company-name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--role", choices=["admin", "user"], default="admin")
    reset = sub.add_parser("reset-password")
    reset.add_argument("--email", required=True)
    args = parser.parse_args()
    try:
        password = read_password()
        if args.command == "create":
            # Validate credentials before initializing a company.
            normalize_email(args.email)
            hash_password(password)
            id = initialize_company(args.company, args.company_name)
            AuthRepository().create_user(id, args.email, password, args.name, args.role)
            print("User created. Sign in through the web page.")
        else:
            digest = hash_password(password)
            with database.SessionLocal() as session, session.begin():
                row = session.scalar(select(User).where(User.email == normalize_email(args.email)))
                if row is None:
                    raise ValueError("User was not found")
                row.password_hash = digest
                session.execute(delete(AuthSession).where(AuthSession.user_id == row.id))
            print("Password changed; existing sessions revoked.")
    except Exception as exc:
        # Never print credentials or database connection/SQL error details.
        parser.exit(1, f"Setup failed: {str(exc) if isinstance(exc, ValueError) else 'check account data and run database migrations first'}\n")


if __name__ == "__main__":
    main()
