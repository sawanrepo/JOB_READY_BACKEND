from pydantic import BaseModel, EmailStr, Field
from datetime import datetime

class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str

class TokenPayload(BaseModel):
    sub: str = None
    exp: int = None

class UserBase(BaseModel):
    email: EmailStr
    full_name: str = None

class UserCreate(UserBase):
    password: str

class UserOut(UserBase):
    id: int
    is_active: bool
    is_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True

class GoogleAuthRequest(BaseModel):
    code: str

class SetPasswordRequest(BaseModel):
    password: str