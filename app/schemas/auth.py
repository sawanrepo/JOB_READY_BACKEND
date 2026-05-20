import re
from pydantic import BaseModel, EmailStr, Field, field_validator
from datetime import datetime

class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str | None = None
    user_id: int
    full_name: str | None = None
    email: str | None = None

class TokenPayload(BaseModel):
    sub: str = None
    exp: int = None

class UserBase(BaseModel):
    email: EmailStr
    full_name: str = None

def validate_password_complexity(v: str) -> str:
    if len(v) < 8:
        raise ValueError('Password must be at least 8 characters long')
    if len(v) > 16:
        raise ValueError('Password must be no more than 16 characters long')
    if not re.search(r'[A-Z]', v):
        raise ValueError('Password must contain at least one uppercase letter')
    if not re.search(r'[a-z]', v):
        raise ValueError('Password must contain at least one lowercase letter')
    if not re.search(r'\d', v):
        raise ValueError('Password must contain at least one number')
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
        raise ValueError('Password must contain at least one special character')
    return v

class UserCreate(UserBase):
    password: str
    
    @field_validator('password')
    @classmethod
    def password_complexity(cls, v):
        return validate_password_complexity(v)

class UserOut(UserBase):
    id: int
    is_active: bool
    is_verified: bool
    is_google_oauth: bool
    created_at: datetime

    class Config:
        from_attributes = True

class GoogleAuthRequest(BaseModel):
    code: str

class SetPasswordRequest(BaseModel):
    password: str
    
    @field_validator('password')
    @classmethod
    def password_complexity(cls, v):
        return validate_password_complexity(v)

class VerifyOTPRequest(BaseModel):
    email: EmailStr
    otp: str

class ResendOTPRequest(BaseModel):
    email: EmailStr

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str
    new_password: str
    
    @field_validator('new_password')
    @classmethod
    def password_complexity(cls, v):
        return validate_password_complexity(v)

class UpdateProfileRequest(BaseModel):
    full_name: str = Field(None, min_length=1, max_length=100)

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    
    @field_validator('new_password')
    @classmethod
    def password_complexity(cls, v):
        return validate_password_complexity(v)
