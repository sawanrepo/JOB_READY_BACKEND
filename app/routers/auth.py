from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas.auth import (
    Token, 
    UserCreate, 
    GoogleAuthRequest,
    SetPasswordRequest,
    VerifyOTPRequest,
    ResendOTPRequest
)
from app.services.auth import (
    authenticate_user,
    create_user,
    handle_google_oauth,
    refresh_access_token,
    set_user_password,
    verify_otp,
    resend_otp
)
from app.utils.security import create_access_token, create_refresh_token
from app.config import settings
from datetime import timedelta
from app.utils.auth import get_current_user
from app.models.user import User

router = APIRouter()

@router.post("/register")
async def register(user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    await create_user(
        email=user_data.email,
        password=user_data.password,
        full_name=user_data.full_name,
        db=db
    )
    
    return {"message": "OTP sent successfully to your email"}

@router.post("/verify-otp", response_model=Token)
async def verify_otp_endpoint(request: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    user = await verify_otp(request.email, request.otp, db)
    
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_refresh_token({"sub": user.email})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token
    }

@router.post("/resend-otp")
async def resend_otp_endpoint(request: ResendOTPRequest, db: AsyncSession = Depends(get_db)):
    await resend_otp(request.email, db)
    return {"message": "A new OTP has been sent to your email"}

@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    user = await authenticate_user(form_data.username, form_data.password, db)
    
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_refresh_token({"sub": user.email})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token
    }

@router.post("/google-auth", response_model=Token)
async def google_auth(
    request: GoogleAuthRequest,
    db: AsyncSession = Depends(get_db)
):
    user = await handle_google_oauth(request.code, db)
    
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_refresh_token({"sub": user.email})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token
    }

@router.post("/refresh", response_model=Token)
async def refresh(refresh_token: str, db: AsyncSession = Depends(get_db)):
    return await refresh_access_token(refresh_token, db)

@router.post("/set-password")
async def set_password(
    request: SetPasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await set_user_password(current_user, request.password, db)
    return {"message": "Password set successfully"}