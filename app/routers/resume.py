from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from app.schemas.resume import ResumeAnalysisResponse, TailoredResumeResponse
from app.services.resume import run_analyze_resume, tailor_resume
from app.routers.auth import get_current_user
from app.models.user import User
from app.utils.file import extract_text_from_pdf
import os
from pathlib import Path
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.utils.usage import can_use_feature, deduct_feature_usage
from datetime import datetime, timedelta, timezone
from app.utils.limiter import limiter

router = APIRouter()

@router.post("/ats-check", response_model=ResumeAnalysisResponse)
@limiter.limit("5/minute")
async def ats_check(
    request: Request,
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not can_use_feature(current_user, "ats_check"):
        raise HTTPException(status_code=403, detail="ATS check limit reached. Upgrade plan or wait for reset.")
    
    resume_text = await extract_text_from_pdf(resume_pdf)
    result =  await run_analyze_resume(resume_text, job_description)

    deduct_feature_usage(current_user, "ats_check")
    await db.commit()
    return result

@router.post("/tailor-resume", response_model=TailoredResumeResponse)
@limiter.limit("5/minute")
async def tailor_resume_endpoint(
    request: Request,
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not can_use_feature(current_user, "resume_tailoring"):
        raise HTTPException(status_code=403, detail="Resume tailoring limit reached. Upgrade plan or wait for reset.")
    result =  await tailor_resume(resume_pdf, job_description)
    deduct_feature_usage(current_user, "resume_tailoring")
    await db.commit()
    return result

OUTPUT_DIR = Path("output").resolve()

@router.get("/download/{filename}")
async def download_resume(
    filename: str,
    current_user: User = Depends(get_current_user),  # Fix #1: require authentication
):
    # Fix #1: prevent path traversal by resolving and checking the path stays inside output/
    safe_path = (OUTPUT_DIR / filename).resolve()
    if not str(safe_path).startswith(str(OUTPUT_DIR)):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(safe_path), media_type="application/pdf")

@router.get("/usage")
async def get_usage(current_user: User = Depends(get_current_user)):
    now = datetime.now(timezone.utc)

    # Calculate reset times
    next_daily_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    days_until_sunday = (6 - now.weekday()) % 7 or 7
    next_weekly_reset = (now + timedelta(days=days_until_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_monthly_reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_monthly_reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    return {
        "ats_checks_left_today": current_user.ats_checks_left_today or 0,
        "resume_tailoring_left_this_week": current_user.resume_tailoring_left_this_week or 0,
        "mock_interviews_left_this_month": current_user.mock_interviews_left_this_month or 0,
        "audio_interviews_left_this_month": current_user.audio_interviews_left_this_month or 0,
        "next_reset_times": {
            "ats_check": next_daily_reset.isoformat(),
            "resume_tailoring": next_weekly_reset.isoformat(),
            "mock_interview": next_monthly_reset.isoformat()
        }
    }