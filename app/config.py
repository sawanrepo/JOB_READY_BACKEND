from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl, EmailStr, Field
from typing import List, Optional

class Settings(BaseSettings):
    DATABASE_URL: str = Field(..., env="DATABASE_URL")
    
    # JWT
    SECRET_KEY: str = Field(..., env="SECRET_KEY")
    ALGORITHM: str = Field(default="HS256", env="ALGORITHM")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, env="ACCESS_TOKEN_EXPIRE_MINUTES")
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, env="REFRESH_TOKEN_EXPIRE_DAYS")
    
    # Google OAuth
    GOOGLE_CLIENT_ID: str = Field(..., env="GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET: str = Field(..., env="GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI: str = Field(..., env="GOOGLE_REDIRECT_URI")
    
    # Email
    SMTP_SERVER: str = Field(..., env="SMTP_SERVER")
    SMTP_PORT: int = Field(..., env="SMTP_PORT")
    SMTP_USERNAME: str = Field(..., env="SMTP_USERNAME")
    SMTP_PASSWORD: str = Field(..., env="SMTP_PASSWORD")
    EMAIL_FROM: EmailStr = Field(..., env="EMAIL_FROM")
    
    # CORS
    CORS_ORIGINS: List[str] = Field(..., env="CORS_ORIGINS")
    
    # Gemini
    GEMINI_API_KEY: str = Field(..., env="GEMINI_API_KEY")

    LATEX_PATH: str = Field(default="pdflatex")
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()