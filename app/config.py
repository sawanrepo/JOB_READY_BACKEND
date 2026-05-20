from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl, EmailStr, Field, field_validator
from typing import List, Optional

class Settings(BaseSettings):
    DATABASE_URL: str = Field(..., env="DATABASE_URL")
    
    # JWT
    SECRET_KEY: str = Field(..., env="SECRET_KEY")
    ALGORITHM: str = Field(default="HS256", env="ALGORITHM")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, env="ACCESS_TOKEN_EXPIRE_MINUTES")
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, env="REFRESH_TOKEN_EXPIRE_DAYS")
    REFRESH_TOKEN_COOKIE_NAME: str = Field(default="refresh_token", env="REFRESH_TOKEN_COOKIE_NAME")
    REFRESH_TOKEN_COOKIE_SECURE: bool = Field(default=True, env="REFRESH_TOKEN_COOKIE_SECURE")
    REFRESH_TOKEN_COOKIE_SAMESITE: str = Field(default="none", env="REFRESH_TOKEN_COOKIE_SAMESITE")
    DEBUG: bool = Field(default=False, env="DEBUG")
    STRICT_UPLOAD_VALIDATION: bool = Field(default=True, env="STRICT_UPLOAD_VALIDATION")
    REQUIRE_FFPROBE: bool = Field(default=True, env="REQUIRE_FFPROBE")
    LATEX_COMPILE_TIMEOUT_SECONDS: int = Field(default=30, env="LATEX_COMPILE_TIMEOUT_SECONDS")
    AWS_TRANSCRIBE_TIMEOUT_SECONDS: int = Field(default=600, env="AWS_TRANSCRIBE_TIMEOUT_SECONDS")
    GEMINI_FILE_PROCESSING_TIMEOUT_SECONDS: int = Field(default=300, env="GEMINI_FILE_PROCESSING_TIMEOUT_SECONDS")
    
    # Google OAuth
    GOOGLE_CLIENT_ID: str = Field(..., env="GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET: str = Field(..., env="GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI: str = Field(..., env="GOOGLE_REDIRECT_URI")
    
    # Email
    SMTP_SERVER: str = Field(..., env="SMTP_SERVER")
    SMTP_PORT: int = Field(..., env="SMTP_PORT")
    SMTP_USE_SSL: bool = Field(default=False, env="SMTP_USE_SSL")
    SMTP_USERNAME: str = Field(..., env="SMTP_USERNAME")
    SMTP_PASSWORD: str = Field(..., env="SMTP_PASSWORD")
    EMAIL_FROM: str = Field(..., env="EMAIL_FROM")
    EMAIL_REPLY_TO: Optional[EmailStr] = Field(default=None, env="EMAIL_REPLY_TO")
    
    # CORS
    CORS_ORIGINS: List[str] = Field(..., env="CORS_ORIGINS")
    
    # Gemini
    GEMINI_API_KEY: str = Field(..., env="GEMINI_API_KEY")

    # Razorpay
    RAZORPAY_KEY_ID: str = Field(..., env="RAZORPAY_KEY_ID")
    RAZORPAY_KEY_SECRET: str = Field(..., env="RAZORPAY_KEY_SECRET")
    RAZORPAY_CREDITING_ENABLED: bool = Field(default=False, env="RAZORPAY_CREDITING_ENABLED")
    RAZORPAY_WEBHOOK_SECRET: str = Field(..., env="RAZORPAY_WEBHOOK_SECRET")

    # AWS
    AWS_ACCESS_KEY_ID: str = Field(default="", env="AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str = Field(default="", env="AWS_SECRET_ACCESS_KEY")
    AWS_REGION: str = Field(default="us-east-1", env="AWS_REGION")
    AWS_S3_BUCKET_NAME: str = Field(default="", env="AWS_S3_BUCKET_NAME")

    LATEX_PATH: str = Field(default="pdflatex")

    @field_validator("DEBUG", mode="before")
    @classmethod
    def debug_must_be_bool_compatible(cls, v):
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"dev", "development"}:
                return True
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_must_be_strong(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long for security")
        return v

    @field_validator("REFRESH_TOKEN_COOKIE_SAMESITE")
    @classmethod
    def refresh_cookie_samesite_must_be_valid(cls, v: str) -> str:
        normalized = v.lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("REFRESH_TOKEN_COOKIE_SAMESITE must be one of: lax, strict, none")
        return normalized

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
