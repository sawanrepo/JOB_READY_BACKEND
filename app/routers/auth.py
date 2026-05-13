from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas.auth import (
    Token, 
    UserCreate, 
    GoogleAuthRequest,
    SetPasswordRequest,
    VerifyOTPRequest,
    ResendOTPRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    UpdateProfileRequest,
    ChangePasswordRequest,
    UserOut
)
from app.services.auth import (
    authenticate_user,
    create_user,
    handle_google_oauth,
    refresh_access_token,
    set_user_password,
    verify_otp,
    resend_otp,
    forgot_password,
    reset_password,
    update_profile,
    change_password
)
from app.utils.security import create_access_token, create_refresh_token
from app.config import settings
from datetime import timedelta
from app.utils.auth import get_current_user
from app.models.user import User
from app.utils.limiter import limiter
from app.utils.logging import log_security_event


router = APIRouter()

@router.post("/register")
@limiter.limit("3/minute")
async def register(request: Request, user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    await create_user(
        email=user_data.email,
        password=user_data.password,
        full_name=user_data.full_name,
        db=db
    )
    
    return {"message": "OTP sent successfully to your email"}

@router.post("/verify-otp", response_model=Token)
async def verify_otp_endpoint(request: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await verify_otp(request.email, request.otp, db)
    except HTTPException as e:
        log_security_event("LOGIN_FAILURE", "anonymous", {"method": "OTP", "email": request.email, "reason": str(e.detail)})
        raise e
    
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_refresh_token({"sub": user.email})
    
    log_security_event("LOGIN_SUCCESS", str(user.id), {"method": "OTP", "email": user.email})

    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "user_id": user.id,
        "full_name": user.full_name,
        "email": user.email
    }

@router.post("/resend-otp")
@limiter.limit("3/minute")
async def resend_otp_endpoint(request: Request, body: ResendOTPRequest, db: AsyncSession = Depends(get_db)):
    await resend_otp(body.email, db)
    return {"message": "A new OTP has been sent to your email"}

@router.post("/forgot-password")
@limiter.limit("3/minute")
async def forgot_password_endpoint(request: Request, body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    await forgot_password(body.email, db)
    return {"message": "If that email exists, a password reset code has been sent."}

@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password_endpoint(request: Request, body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    await reset_password(body.email, body.otp, body.new_password, db)
    return {"message": "Password has been successfully reset. You can now log in."}

@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.patch("/me", response_model=UserOut)
async def update_me(
    request: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    return await update_profile(current_user, request.full_name, db)

@router.post("/change-password")
async def change_password_endpoint(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await change_password(current_user, request.current_password, request.new_password, db)
    return {"message": "Password changed successfully"}

@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    try:
        user = await authenticate_user(form_data.username, form_data.password, db)
    except HTTPException as e:
        log_security_event("LOGIN_FAILURE", "anonymous", {"method": "PASSWORD", "email": form_data.username, "reason": str(e.detail)})
        raise e
    
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_refresh_token({"sub": user.email})
    
    log_security_event("LOGIN_SUCCESS", str(user.id), {"method": "PASSWORD", "email": user.email})

    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "user_id": user.id,
        "full_name": user.full_name,
        "email": user.email
    }

@router.post("/google-auth", response_model=Token)
async def google_auth(
    request: GoogleAuthRequest,
    db: AsyncSession = Depends(get_db)
):
    try:
        user = await handle_google_oauth(request.code, db)
    except HTTPException as e:
        log_security_event("LOGIN_FAILURE", "anonymous", {"method": "GOOGLE", "reason": str(e.detail)})
        raise e
    
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_refresh_token({"sub": user.email})
    
    log_security_event("LOGIN_SUCCESS", str(user.id), {"method": "GOOGLE", "email": user.email})

    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "user_id": user.id,
        "full_name": user.full_name,
        "email": user.email
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