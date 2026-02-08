import logging
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
from app.routers import auth, resume, payment, interview
from app.config import settings
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Job Ready API",
    description="Backend for Job Ready application",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/resume/download", StaticFiles(directory="output"), name="resume-download")
# Include routers
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(resume.router, prefix="/resume", tags=["Resume"])
app.include_router(payment.router, prefix="/payment", tags=["Payment"])
app.include_router(interview.router, prefix="/interview", tags=["Interview"])

@app.get("/")
def root():
    return {"message": "Job Ready API is running"}