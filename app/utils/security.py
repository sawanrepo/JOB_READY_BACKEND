from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


from typing import Optional

def verify_password(plain_password: str, hashed_password: Optional[bytes]) -> bool:
    if not hashed_password:
        return False
    return pwd_context.verify(plain_password, hashed_password.decode('utf-8'))


def get_password_hash(password: str) -> bytes:
    return pwd_context.hash(password).encode('utf-8')


def hash_otp(otp: str) -> str:
    """Hash an OTP for secure storage (stored as a string in DB)."""
    return pwd_context.hash(otp)


def verify_otp_hash(plain_otp: str, hashed_otp: str) -> bool:
    """Verify a plain OTP against its bcrypt hash."""
    try:
        return pwd_context.verify(plain_otp, hashed_otp)
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    # Include token type so refresh tokens cannot be used as access tokens
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )