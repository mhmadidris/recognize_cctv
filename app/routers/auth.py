from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from email.message import EmailMessage
import hashlib
import secrets
import smtplib
from passlib.context import CryptContext
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from app.models.user import User
from app.models.password_reset import PasswordResetToken
from app.utils.database import SessionLocal, get_db
from app.utils.response import success_response, error_response
import os
from app.models.company import Company
from app.services.cctv_recognition import _env

APP_MODE = os.getenv("APP_MODE", "multi_tenant")

router = APIRouter(prefix="/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-key-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 # 7 days

class UserRegister(BaseModel):
    email: str
    password: str
    full_name: str

class UserSetup(BaseModel):
    account_type: str # "personal" or "company"
    company_name: Optional[str] = None
    company_phone: Optional[str] = None

class UserLogin(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str

def get_password_hash(password):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def _send_password_reset_email(email: str, reset_url: str):
    smtp_host = _env("SMTP_HOST", "")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")

    message = EmailMessage()
    message["Subject"] = "Reset password akun Anda"
    message["From"] = _env("SMTP_FROM", _env("SMTP_USER", ""))
    message["To"] = email
    message.set_content(
        "Kami menerima permintaan reset password. Buka link berikut dalam 30 menit:\n\n"
        f"{reset_url}\n\nJika Anda tidak meminta reset password, abaikan email ini."
    )

    port = int(_env("SMTP_PORT", "587"))
    with smtplib.SMTP(smtp_host, port, timeout=20) as smtp:
        if _env("SMTP_TLS", "true").lower() in {"1", "true", "yes", "on"}:
            smtp.starttls()
        smtp_user = _env("SMTP_USER", "")
        if smtp_user:
            smtp.login(smtp_user, _env("SMTP_PASSWORD", ""))
        smtp.send_message(message)


def _token_hash(token: str):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

from app.models.company import Company
import uuid

@router.post("/register", response_model=dict)
def register(req: UserRegister):
    with SessionLocal() as db:
        if db.query(User).filter(User.email == req.email).first():
            raise HTTPException(status_code=400, detail="Email already registered")
        
        hashed_password = get_password_hash(req.password)
        new_user = User(
            email=req.email,
            password_hash=hashed_password,
            full_name=req.full_name,
        )
        if APP_MODE == "single_tenant":
            default_company = db.query(Company).first()
            if not default_company:
                default_company = Company(name="Main Company", phone="")
                db.add(default_company)
                db.commit()
                db.refresh(default_company)
            
            new_user.company_id = default_company.id
            new_user.account_type = "company"

        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        token = create_access_token(data={"sub": str(new_user.id)}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
        return success_response(result={"token": token, "user": {"id": str(new_user.id), "email": new_user.email, "full_name": new_user.full_name, "account_type": new_user.account_type, "company_id": str(new_user.company_id) if new_user.company_id else None}}, message="Registered successfully")

@router.post("/login", response_model=dict)
def login(req: UserLogin):
    try:
        with SessionLocal() as db:
            user = db.query(User).filter(func.lower(User.email) == req.email.strip().lower()).first()
            if not user:
                raise HTTPException(
                    status_code=404,
                    detail="Email belum terdaftar. Silakan daftar terlebih dahulu.",
                )
            try:
                valid_password = verify_password(req.password, user.password_hash)
            except Exception:
                valid_password = False
            if not valid_password:
                raise HTTPException(status_code=400, detail="Incorrect email or password")

            token = create_access_token(
                data={"sub": str(user.id)},
                expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            )
            return success_response(
                result={
                    "token": token,
                    "user": {
                        "id": str(user.id),
                        "email": user.email,
                        "full_name": user.full_name,
                        "company_id": str(user.company_id) if user.company_id else None,
                        "account_type": user.account_type,
                    },
                },
                message="Login successful",
            )
    except HTTPException:
        raise
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="Database login service unavailable.") from error


@router.post("/forgot-password", response_model=dict)
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    generic_message = "Jika email terdaftar, link reset password telah dikirim."
    email = req.email.strip()
    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
    if not user:
        return success_response(result={"sent": True}, message=generic_message)

    raw_token = secrets.token_urlsafe(48)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": datetime.utcnow()})
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_token_hash(raw_token),
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
    )
    db.commit()

    frontend_url = _env("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    reset_url = f"{frontend_url}/reset-password?token={raw_token}"
    try:
        _send_password_reset_email(user.email, reset_url)
    except RuntimeError as error:
        if _env("IS_DEBUG", "false").lower() in {"1", "true", "yes", "on"}:
            print(f"Password reset email unavailable: {error}. Reset URL: {reset_url}")
        else:
            raise HTTPException(status_code=503, detail="Layanan email belum dikonfigurasi.") from error
    return success_response(result={"sent": True}, message=generic_message)


@router.post("/reset-password", response_model=dict)
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    if len(req.password) < 8:
        raise HTTPException(status_code=422, detail="Password minimal 8 karakter")

    reset_token = db.query(PasswordResetToken).filter(
        PasswordResetToken.token_hash == _token_hash(req.token),
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.expires_at > datetime.utcnow(),
    ).first()
    if not reset_token:
        raise HTTPException(status_code=400, detail="Token reset tidak valid atau sudah kedaluwarsa")

    user = db.query(User).filter(User.id == reset_token.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Token reset tidak valid")
    user.password_hash = get_password_hash(req.password)
    reset_token.used_at = datetime.utcnow()
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.id != reset_token.id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": datetime.utcnow()})
    db.commit()
    return success_response(result={"reset": True}, message="Password berhasil diubah. Silakan masuk kembali.")

from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# Dependency for protected routes
def get_current_user_id(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

@router.post("/setup-account", response_model=dict)
def setup_account(req: UserSetup, db: Session = Depends(get_db), current_user_id: str = Depends(get_current_user_id)):
    user = db.query(User).filter(User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user.account_type:
        raise HTTPException(status_code=400, detail="Account already setup")
        
    if req.account_type == "company":
        new_company = Company(
            name=req.company_name or "My Company",
            phone=req.company_phone
        )
        db.add(new_company)
        db.commit()
        db.refresh(new_company)
        user.company_id = new_company.id
        
    user.account_type = req.account_type
    db.commit()
    db.refresh(user)
    
    return success_response(result={"user": {"id": str(user.id), "email": user.email, "full_name": user.full_name, "company_id": str(user.company_id) if user.company_id else None, "account_type": user.account_type}}, message="Account setup successful")
