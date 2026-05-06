from sqlalchemy.future import select
from sqlalchemy import update
from app.database import get_db
from app.models.user import User
from app.utils.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    verify_token,
    hash_otp,
    verify_otp_hash,
)
from app.schemas.auth import Token
from fastapi import HTTPException, status
from datetime import timedelta, datetime, timezone
from app.config import settings
import httpx
from jose import JWTError
import random
import string
import asyncio
from app.utils.email import send_otp_email, send_password_reset_email


def generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))


async def authenticate_user(email: str, password: str, db) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email not verified. Please verify your email first.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )

    return user


async def create_user(email: str, password: str, full_name: str, db) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    otp = generate_otp()
    otp_hash = hash_otp(otp)          # Fix #10: store hash, not plaintext
    hashed_password = get_password_hash(password)

    if user:
        if user.is_verified:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        else:
            # Overwrite unverified user
            user.hashed_password = hashed_password
            user.full_name = full_name
            user.otp = otp_hash
            user.otp_created_at = datetime.now(timezone.utc)
    else:
        user = User(
            email=email,
            hashed_password=hashed_password,
            full_name=full_name,
            is_active=True,
            is_verified=False,
            otp=otp_hash,
            otp_created_at=datetime.now(timezone.utc)
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)

    # Fix #7: run blocking SMTP call in a thread so it doesn't block the event loop
    await asyncio.to_thread(send_otp_email, email, otp)

    return user


async def verify_otp(email: str, otp: str, db) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.is_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already verified")

    # Fix #4: use hash verification instead of plain == comparison
    if not user.otp or not verify_otp_hash(otp, user.otp):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")

    if not user.otp_created_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP expired or invalid")

    # Check if OTP is expired (15 minutes)
    time_diff = datetime.now(timezone.utc) - user.otp_created_at
    if time_diff > timedelta(minutes=15):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code has expired")

    # Mark user as verified
    user.is_verified = True
    user.otp = None
    user.otp_created_at = None
    await db.commit()
    await db.refresh(user)

    return user


async def resend_otp(email: str, db) -> None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    # Fix #17: don't reveal whether email exists; silently succeed for unknown/verified emails
    if not user or user.is_verified:
        return

    # Check 2-minute cooldown
    if user.otp_created_at:
        time_diff = datetime.now(timezone.utc) - user.otp_created_at
        if time_diff < timedelta(minutes=2):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait 2 minutes before requesting a new code"
            )

    otp = generate_otp()
    user.otp = hash_otp(otp)          # Fix #10: store hash
    user.otp_created_at = datetime.now(timezone.utc)
    await db.commit()

    # Fix #7: non-blocking SMTP call
    await asyncio.to_thread(send_otp_email, email, otp)


async def forgot_password(email: str, db) -> None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    # Fix #3: don't reveal whether the email exists in the system
    if not user or (user.is_google_oauth and not user.hashed_password):
        return

    # Check 2-minute cooldown
    if user.otp_created_at:
        time_diff = datetime.now(timezone.utc) - user.otp_created_at
        if time_diff < timedelta(minutes=2):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait 2 minutes before requesting a new code"
            )

    otp = generate_otp()
    user.otp = hash_otp(otp)          # Fix #10: store hash
    user.otp_created_at = datetime.now(timezone.utc)
    await db.commit()

    # Fix #7: non-blocking SMTP call
    await asyncio.to_thread(send_password_reset_email, email, otp)


async def reset_password(email: str, otp: str, new_password: str, db) -> None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Fix #4: use hash verification instead of plain == comparison
    if not user.otp or not verify_otp_hash(otp, user.otp):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reset code")

    if not user.otp_created_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Code expired or invalid")

    # Check if OTP is expired (15 minutes)
    time_diff = datetime.now(timezone.utc) - user.otp_created_at
    if time_diff > timedelta(minutes=15):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset code has expired")

    user.is_verified = True
    user.hashed_password = get_password_hash(new_password)
    user.otp = None
    user.otp_created_at = None
    await db.commit()


async def update_profile(user: User, full_name: str, db) -> User:
    if full_name:
        user.full_name = full_name
        await db.commit()
    return user


async def change_password(user: User, current_password: str, new_password: str, db) -> User:
    if user.is_google_oauth and not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You signed up with Google. Please use Google to log in."
        )

    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect current password."
        )

    user.hashed_password = get_password_hash(new_password)
    await db.commit()
    return user


async def handle_google_oauth(code: str, db) -> User:
    token_url = "https://oauth2.googleapis.com/token"
    user_info_url = "https://www.googleapis.com/oauth2/v3/userinfo"

    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_response = await client.post(
            token_url,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code"
            }
        )

        token_data = token_response.json()
        if "access_token" not in token_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to authenticate with Google"
            )

        # Get user info
        user_info_response = await client.get(
            user_info_url,
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        user_info = user_info_response.json()

        # Fix #16: reject unverified Google emails
        if not user_info.get("email_verified"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google account email is not verified"
            )

        # Find or create user
        result = await db.execute(select(User).where(User.email == user_info["email"]))
        user = result.scalars().first()

        if user:
            if not user.is_google_oauth:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered with password"
                )
            return user

        # Create new user
        new_user = User(
            email=user_info["email"],
            full_name=user_info.get("name", ""),
            is_google_oauth=True,
            google_id=user_info["sub"],
            is_active=True,
            is_verified=True,
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
        return new_user


async def refresh_access_token(refresh_token: str, db) -> Token:
    try:
        payload = verify_token(refresh_token)
        email: str = payload.get("sub")
        # Fix #12: ensure only genuine refresh tokens are accepted
        token_type: str = payload.get("type")
        if email is None or token_type != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )

        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )

        access_token = create_access_token(
            data={"sub": user.email},
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        return Token(access_token=access_token, token_type="bearer", refresh_token=refresh_token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )


async def set_user_password(user: User, password: str, db) -> None:
    if not user.is_google_oauth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password already set for this account"
        )

    hashed_password = get_password_hash(password)
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(hashed_password=hashed_password)
    )
    await db.commit()