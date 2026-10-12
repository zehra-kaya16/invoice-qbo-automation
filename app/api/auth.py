from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from typing import Literal, Optional

from app.core.auth import COOKIE_NAME, Principal, check_origin, company_access, require_admin
from app.core.config import settings
from app.services.persistence.auth_repository import AuthRepository

router = APIRouter()


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class UserCreate(LoginRequest):
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    role: Literal["admin", "user"] = "user"


class UserChange(BaseModel):
    is_active: Optional[bool] = None
    role: Optional[Literal["admin", "user"]] = None


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=128)


def storage(operation, *args):
    try:
        return operation(*args)
    except IntegrityError:
        raise HTTPException(409, "Account information changed; reload before continuing") from None
    except SQLAlchemyError:
        raise HTTPException(503, "Authentication storage is unavailable") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    check_origin(request)
    token = storage(AuthRepository().login, body.email, body.password, request.client.host if request.client else "unknown")
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.auth_cookie_secure,
                        samesite="lax", path="/", max_age=max(1, min(settings.auth_session_hours, 24)) * 3600)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "signed_in"}


@router.get("/me")
def me(response: Response, user: Principal = Depends(company_access)):
    response.headers["Cache-Control"] = "no-store"
    return {"user_id": user.user_id, "email": user.email, "name": user.name, "role": user.role,
            "company": {"id": user.company_id, "key": user.company_key, "name": user.company_name}, "csrf_token": user.csrf_token}


@router.post("/logout")
def logout(response: Response, user: Principal = Depends(company_access)):
    storage(AuthRepository().logout, user)
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.auth_cookie_secure, httponly=True, samesite="lax")
    return {"status": "signed_out"}


@router.get("/users", dependencies=[Depends(company_access)])
def users(user: Principal = Depends(require_admin)):
    return {"items": storage(AuthRepository().users, user.company_id)}


@router.post("/users", dependencies=[Depends(company_access)])
def create_user(body: UserCreate, user: Principal = Depends(require_admin)):
    return storage(AuthRepository().create_user, user.company_id, body.email, body.password, body.name, body.role)


@router.patch("/users/{id}", dependencies=[Depends(company_access)])
def change_user(id: str, body: UserChange, user: Principal = Depends(require_admin)):
    return storage(AuthRepository().change_user, user, id, body.is_active, body.role)


@router.post("/password")
def change_password(body: PasswordChange, response: Response, user: Principal = Depends(company_access)):
    storage(AuthRepository().change_password, user, body.old_password, body.password)
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.auth_cookie_secure, httponly=True, samesite="lax")
    return {"status": "password_changed_sign_in_again"}
