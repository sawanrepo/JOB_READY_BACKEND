from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.services.audio_interview_service import AudioInterviewService
from app.utils.auth import get_current_user
from app.utils.usage import can_use_feature, deduct_feature_usage
from app.utils.validation import validate_job_description, validate_resume_text

from app.models.user import User

from app.models.interview import InterviewResult as DBInterviewResult
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form, Request
import os
import uuid
import logging
import aiofiles
from sqlalchemy import delete
from app.utils.limiter import limiter
from app.utils.file import extract_text_from_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["Audio Interview"])
audio_interview_service = AudioInterviewService()

TEMP_DIR = "temp_audios"
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_AUDIO_SIZE_BYTES = 100 * 1024 * 1024 # 100MB max for merged audio

@router.post("/start")
@limiter.limit("5/minute")
async def start_audio_interview(
    request: Request,
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Check usage
        if not can_use_feature(current_user, "audio_interview"):
            raise HTTPException(status_code=403, detail="Audio interview limit reached. Please upgrade or purchase one.")

        validate_job_description(job_description)


        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)
        req = InterviewStartRequest(resume_text=resume_text, job_description=job_description)


        response = await audio_interview_service.start_audio_interview(req)

        # Deduct usage only if successful
        deduct_feature_usage(current_user, "audio_interview")
        await db.commit()

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting audio interview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{session_id}/result", response_model=InterviewResult)
@limiter.limit("5/minute")
async def get_audio_result(
    request: Request,
    session_id: str,
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    ext = os.path.splitext(audio.filename or "")[1] or ".webm"
    safe_ext = ext if ext.lower() in {".webm", ".mp3", ".wav", ".m4a", ".ogg"} else ".webm"
    temp_path = os.path.join(TEMP_DIR, f"{uuid.uuid4()}{safe_ext}")

    try:
        content = await audio.read(MAX_AUDIO_SIZE_BYTES + 1)
        if len(content) > MAX_AUDIO_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="Audio file too large. Maximum allowed size is 100 MB.")

        async with aiofiles.open(temp_path, "wb") as out_file:
            await out_file.write(content)

        logger.info(f"Saved merged audio to {temp_path}")
        
        result_data = await audio_interview_service.generate_result(session_id, temp_path)
        
        # Delete previous audio results for this user
        delete_stmt = delete(DBInterviewResult).where(
            DBInterviewResult.user_id == current_user.id,
            DBInterviewResult.interview_type == "audio"
        )
        await db.execute(delete_stmt)
        
        # Save to DB
        db_result = DBInterviewResult(
            user_id=current_user.id,
            interview_type="audio",
            result_data=result_data
        )
        db.add(db_result)
        await db.commit()
        
        return result_data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating audio result: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # User requirement: "delete audio as soon as we get result from ai"
        if os.path.exists(temp_path):
            os.remove(temp_path)
            logger.info(f"Deleted temp audio file {temp_path}")
