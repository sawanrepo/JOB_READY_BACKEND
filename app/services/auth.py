from sqlalchemy.future import select
from sqlalchemy import update
from app.database import get_db
from app.models.user import User
from app.utils.security import (
    verify_password, 
    get_password_hash, 
    create_access_token,
    create_refresh_token,
    verify_token
)
from app.schemas.auth import Token
from fastapi import HTTPException, status
from datetime import timedelta
from app.config import settings
import httpx
from jose import JWTError

async def authenticate_user(email: str, password: str, db) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )
    
    return user

async def create_user(email: str, password: str, full_name: str, db) -> User:
    result = await db.execute(select(User).where(User.email == email))
    if result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
    hashed_password = get_password_hash(password)
    user = User(
        email=email,
        hashed_password=hashed_password,
        full_name=full_name,
        is_active=True
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
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
            is_active=True
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
        return new_user

async def refresh_access_token(refresh_token: str, db) -> Token:
    try:
        payload = verify_token(refresh_token)
        email: str = payload.get("sub")
        if email is None:
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