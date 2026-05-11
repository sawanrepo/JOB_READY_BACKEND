from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.services.interview_service import InterviewService
from app.utils.auth import get_current_user
from app.utils.usage import can_use_feature, deduct_feature_usage
from app.utils.validation import validate_job_description, validate_resume_text

from app.models.user import User

from app.models.interview import InterviewResult as DBInterviewResult
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, APIRouter, UploadFile, File, HTTPException, Form, Request
from datetime import datetime, timezone
import shutil
import os
import uuid
import logging
from sqlalchemy import select, desc, delete
from app.utils.limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter()
interview_service = InterviewService()

from app.utils.file import extract_text_from_pdf

TEMP_DIR = "temp_videos"
os.makedirs(TEMP_DIR, exist_ok=True)

# Fix #15: maximum allowed video upload size (50 MB)
MAX_VIDEO_SIZE_BYTES = 50 * 1024 * 1024


@router.post("/start", response_model=InterviewResponse)
@limiter.limit("5/minute")
async def start_interview(
    request: Request,
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Check usage
        if not can_use_feature(current_user, "mock_interview"):
            raise HTTPException(status_code=403, detail="Mock interview limit reached. Please upgrade or wait for reset.")

        validate_job_description(job_description)


        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)
        from app.schemas.interview import InterviewStartRequest

        req = InterviewStartRequest(resume_text=resume_text, job_description=job_description)

        response = await interview_service.start_interview(req)

        # Deduct usage only if successful
        deduct_feature_usage(current_user, "mock_interview")
        await db.commit()

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting interview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


import aiofiles


@router.post("/{session_id}/response", response_model=InterviewResponse)
@limiter.limit("10/minute")
async def process_response(
    request: Request,
    session_id: str,
    video: UploadFile = File(...),
    # Fix #2: require authenticated user so sessions cannot be hijacked
    current_user: User = Depends(get_current_user),
):
    # Fix #8: use a server-generated UUID filename, never trust client-supplied filenames
    ext = os.path.splitext(video.filename or "")[1] or ".webm"
    safe_ext = ext if ext.lower() in {".webm", ".mp4", ".ogg", ".mkv"} else ".webm"
    temp_path = os.path.join(TEMP_DIR, f"{uuid.uuid4()}{safe_ext}")

    try:
        # Fix #15: enforce upload size limit before writing to disk
        content = await video.read(MAX_VIDEO_SIZE_BYTES + 1)
        if len(content) > MAX_VIDEO_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="Video file too large. Maximum allowed size is 50 MB.")

        async with aiofiles.open(temp_path, "wb") as out_file:
            await out_file.write(content)

        logger.info(f"Saved video to {temp_path}")
        response = await interview_service.process_response(session_id, temp_path)
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing response: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@router.get("/history/latest")
@limiter.limit("5/minute")
async def get_latest_history(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Fetch latest video
        video_stmt = select(DBInterviewResult).where(
            DBInterviewResult.user_id == current_user.id,
            DBInterviewResult.interview_type == "video"
        ).order_by(desc(DBInterviewResult.created_at)).limit(1)
        video_result = (await db.execute(video_stmt)).scalar_one_or_none()
        
        # Fetch latest audio
        audio_stmt = select(DBInterviewResult).where(
            DBInterviewResult.user_id == current_user.id,
            DBInterviewResult.interview_type == "audio"
        ).order_by(desc(DBInterviewResult.created_at)).limit(1)
        audio_result = (await db.execute(audio_stmt)).scalar_one_or_none()
        
        return {
            "video": video_result.result_data if video_result else None,
            "audio": audio_result.result_data if audio_result else None
        }
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/{session_id}/result", response_model=InterviewResult)
@limiter.limit("5/minute")
async def get_result(
    request: Request,
    session_id: str,
    # Fix #2: require authenticated user
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        result_data = await interview_service.generate_result(session_id)
        
        # Delete previous video results for this user
        delete_stmt = delete(DBInterviewResult).where(
            DBInterviewResult.user_id == current_user.id,
            DBInterviewResult.interview_type == "video"
        )
        await db.execute(delete_stmt)
        
        # Save to DB
        db_result = DBInterviewResult(
            user_id=current_user.id,
            interview_type="video",
            result_data=result_data
        )
        db.add(db_result)
        await db.commit()
        
        return result_data
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating result: {e}")
        raise HTTPException(status_code=500, detail=str(e))
