from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.services.interview_service import InterviewService
from app.utils.auth import get_current_user
from app.utils.usage import can_use_feature_async, deduct_feature_usage_atomic

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

@router.get("/active-sessions")
@limiter.limit("5/minute")
async def get_active_sessions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns any active (unfinished) interview sessions for the user."""
    stmt = select(InterviewSession).where(
        InterviewSession.user_id == current_user.id,
        InterviewSession.is_active == True
    ).order_by(desc(InterviewSession.created_at))
    
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    
    return [
        {
            "id": s.id,
            "type": s.interview_type,
            "created_at": s.created_at,
            "question_number": s.question_number
        } for s in sessions
    ]

@router.get("/session/{session_id}", response_model=InterviewResponse)
@limiter.limit("10/minute")
async def get_session_status(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):

    """Fetches the current state of an active interview session."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    return InterviewResponse(
        session_id=session.id,
        question=session.current_question or "Starting interview...",
        question_number=session.question_number,
        total_questions=10 if session.interview_type == "audio" else 8,
        interview_ended=not session.is_active,
        warnings_count=session.warnings_count or 0,
        is_active=session.is_active
    )



from app.utils.file import extract_text_from_pdf, validate_file_security, ALLOWED_VIDEO_EXTENSIONS
from app.models.interview import InterviewSession

TEMP_DIR = "temp_videos"
os.makedirs(TEMP_DIR, exist_ok=True)

# MAX_VIDEO_SIZE_BYTES is now handled by utility



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
        if not await can_use_feature_async(db, current_user.id, "mock_interview"):
            raise HTTPException(status_code=403, detail="Mock interview limit reached. Please upgrade or wait for reset.")


        validate_job_description(job_description)


        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)
        from app.schemas.interview import InterviewStartRequest

        req = InterviewStartRequest(resume_text=resume_text, job_description=job_description)

        response = await interview_service.start_interview(db, current_user.id, req)


        # Deduct usage only if successful
        await deduct_feature_usage_atomic(db, current_user.id, "mock_interview")
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
    db: AsyncSession = Depends(get_db)
):

    # Fix #8: use a server-generated UUID filename, never trust client-supplied filenames
    ext = os.path.splitext(video.filename or "")[1] or ".webm"
    safe_ext = ext if ext.lower() in {".webm", ".mp4", ".ogg", ".mkv"} else ".webm"
    temp_path = os.path.join(TEMP_DIR, f"{uuid.uuid4()}{safe_ext}")

    try:
        # Enforce security validation
        validate_file_security(video, ALLOWED_VIDEO_EXTENSIONS, max_size_mb=50)

        content = await video.read()
        async with aiofiles.open(temp_path, "wb") as out_file:

            await out_file.write(content)

        logger.info(f"Saved video to {temp_path}")
        response = await interview_service.process_response(db, session_id, temp_path)
        await db.commit() # Commit history updates

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
@limiter.limit("10/minute")
async def get_result_by_session(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches a specific interview result from history using session_id. 
    Works for both audio and video interviews.
    """
    stmt = select(DBInterviewResult).where(
        DBInterviewResult.session_id == session_id,
        DBInterviewResult.user_id == current_user.id
    )
    result = await db.execute(stmt)
    db_result = result.scalar_one_or_none()
    
    if db_result:
        return db_result.result_data

    # Fallback: check if session still exists and needs generation
    session_stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id
    )
    session_res = await db.execute(session_stmt)
    session = session_res.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Result or session not found")
        
    # If session exists but result doesn't, trigger generation based on type
    if session.interview_type == "video":
        return await get_result(request, session_id, current_user, db)
    elif session.interview_type == "audio":
        from app.routers.audio_interview import get_audio_result
        return await get_audio_result(request, session_id, current_user, db)
    
    raise HTTPException(status_code=404, detail="Unknown interview type")


@router.post("/{session_id}/result", response_model=InterviewResult)
@limiter.limit("5/minute")
async def get_result(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Check if result already exists (avoid re-generating if refresh happens during generation)
        existing_stmt = select(DBInterviewResult).where(
            DBInterviewResult.session_id == session_id,
            DBInterviewResult.user_id == current_user.id
        )
        existing_res = await db.execute(existing_stmt)
        if existing_db_res := existing_res.scalar_one_or_none():
            return existing_db_res.result_data

        result_data = await interview_service.generate_result(db, session_id)

        # Delete previous video results for this user
        delete_stmt = delete(DBInterviewResult).where(
            DBInterviewResult.user_id == current_user.id,
            DBInterviewResult.interview_type == "video"
        )
        await db.execute(delete_stmt)
        
        # Save to DB with session_id
        db_result = DBInterviewResult(
            user_id=current_user.id,
            session_id=session_id, # Persistent for refresh
            interview_type="video",
            result_data=result_data
        )
        db.add(db_result)
        
        # Cleanup: Delete the persistent session after successful result generation
        session_delete_stmt = delete(InterviewSession).where(InterviewSession.id == session_id)
        await db.execute(session_delete_stmt)
        
        await db.commit()
        
        return result_data
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating result: {e}")
        raise HTTPException(status_code=500, detail=str(e))
