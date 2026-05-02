from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.services.interview_service import InterviewService
from app.utils.auth import get_current_user
from app.utils.usage import can_use_feature, deduct_feature_usage
from app.models.user import User
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, APIRouter, UploadFile, File, HTTPException, Form
from datetime import datetime, timezone
import shutil
import os
import logging

logger = logging.getLogger(__name__)

router = APIRouter()
interview_service = InterviewService()

from app.utils.file import extract_text_from_pdf

TEMP_DIR = "temp_videos"
os.makedirs(TEMP_DIR, exist_ok=True)

@router.post("/start", response_model=InterviewResponse)
async def start_interview(
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Check usage
        if not can_use_feature(current_user, "mock_interview"):
            raise HTTPException(status_code=403, detail="Mock interview limit reached. Please upgrade or wait for reset.")

        resume_text = await extract_text_from_pdf(resume_pdf)
        from app.schemas.interview import InterviewStartRequest
        request = InterviewStartRequest(resume_text=resume_text, job_description=job_description)
        
        # Deduct usage
        deduct_feature_usage(current_user, "mock_interview")
        await db.commit()
        
        return await interview_service.start_interview(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting interview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

import aiofiles

@router.post("/{session_id}/response", response_model=InterviewResponse)
async def process_response(
    session_id: str,
    video: UploadFile = File(...)
):
    temp_path = os.path.join(TEMP_DIR, f"{session_id}_{video.filename}")
    try:
        async with aiofiles.open(temp_path, "wb") as out_file:
            content = await video.read()
            await out_file.write(content)
            
        logger.info(f"Saved video to {temp_path}")
        response = await interview_service.process_response(session_id, temp_path)
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error processing response: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)

@router.get("/{session_id}/result", response_model=InterviewResult)
async def get_result(session_id: str):
    try:
        return await interview_service.generate_result(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating result: {e}")
        raise HTTPException(status_code=500, detail=str(e))
