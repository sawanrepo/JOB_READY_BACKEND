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
from sqlalchemy import select, delete, desc
from app.database import get_db
from app.models.resume import ResumeHistory as DBResumeHistory
from app.utils.usage import can_use_feature, deduct_feature_usage
from app.utils.validation import validate_job_description, validate_resume_text

from datetime import datetime, timedelta, timezone

from app.utils.limiter import limiter

router = APIRouter()
OUTPUT_DIR = Path("output").resolve()

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
    
    validate_job_description(job_description)

    
    resume_text = await extract_text_from_pdf(resume_pdf)
    validate_resume_text(resume_text)
    result =  await run_analyze_resume(resume_text, job_description)


    deduct_feature_usage(current_user, "ats_check")
    
    # Update History: Delete old ATS and save new one
    delete_stmt = delete(DBResumeHistory).where(
        DBResumeHistory.user_id == current_user.id,
        DBResumeHistory.history_type == "ats"
    )
    await db.execute(delete_stmt)
    
    new_history = DBResumeHistory(
        user_id=current_user.id,
        history_type="ats",
        result_data=result.model_dump() if hasattr(result, "model_dump") else result
    )
    db.add(new_history)
    
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
    
    validate_job_description(job_description)
    
    resume_text = await extract_text_from_pdf(resume_pdf)
    validate_resume_text(resume_text)

    result =  await tailor_resume(resume_pdf, job_description)
    deduct_feature_usage(current_user, "resume_tailoring")
    
    # Update History: Delete old tailor and save new one
    # First, get the old filename to delete the file
    old_tailor_stmt = select(DBResumeHistory).where(
        DBResumeHistory.user_id == current_user.id,
        DBResumeHistory.history_type == "tailor"
    )
    old_tailor = (await db.execute(old_tailor_stmt)).scalar_one_or_none()
    
    if old_tailor and old_tailor.file_path:
        old_file_path = OUTPUT_DIR / old_tailor.file_path
        if old_file_path.exists():
            try:
                os.remove(old_file_path)
            except Exception as e:
                logger.error(f"Error deleting old resume file: {e}")

    delete_stmt = delete(DBResumeHistory).where(
        DBResumeHistory.user_id == current_user.id,
        DBResumeHistory.history_type == "tailor"
    )
    await db.execute(delete_stmt)
    
    new_history = DBResumeHistory(
        user_id=current_user.id,
        history_type="tailor",
        result_data={"filename": result["filename"]}, # Store metadata
        file_path=result["filename"] # Link to the file in output/
    )
    db.add(new_history)
    
    await db.commit()
    return result

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

@router.get("/history/latest")
async def get_latest_resume_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Fetch latest ATS
        ats_stmt = select(DBResumeHistory).where(
            DBResumeHistory.user_id == current_user.id,
            DBResumeHistory.history_type == "ats"
        ).order_by(desc(DBResumeHistory.created_at)).limit(1)
        ats_record = (await db.execute(ats_stmt)).scalar_one_or_none()
        
        # Fetch latest Tailor
        tailor_stmt = select(DBResumeHistory).where(
            DBResumeHistory.user_id == current_user.id,
            DBResumeHistory.history_type == "tailor"
        ).order_by(desc(DBResumeHistory.created_at)).limit(1)
        tailor_record = (await db.execute(tailor_stmt)).scalar_one_or_none()
        
        return {
            "ats": {
                "result": ats_record.result_data if ats_record else None,
                "created_at": ats_record.created_at.isoformat() if ats_record else None
            },
            "tailor": {
                "filename": tailor_record.file_path if tailor_record else None,
                "created_at": tailor_record.created_at.isoformat() if tailor_record else None
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
        "ats_checks_left_today": (current_user.ats_checks_left_today or 0) + (current_user.purchased_ats_credits or 0),
        "resume_tailoring_left_this_week": (current_user.resume_tailoring_left_this_week or 0) + (current_user.purchased_tailor_credits or 0),
        "purchased_ats_credits": current_user.purchased_ats_credits or 0,
        "purchased_tailor_credits": current_user.purchased_tailor_credits or 0,
        "mock_interviews_left": current_user.mock_interviews_left or 0,
        "audio_interviews_left": current_user.audio_interviews_left or 0,
        "next_reset_times": {
            "ats_check": next_daily_reset.isoformat(),
            "resume_tailoring": next_weekly_reset.isoformat(),
        }
    }