from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form, Request, BackgroundTasks
import os
import uuid
import logging
import aiofiles
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.services.audio_interview_service import AudioInterviewService
from app.utils.auth import get_current_user
from app.utils.usage import can_use_feature_async, deduct_feature_usage_atomic
from app.utils.validation import validate_job_description, validate_resume_text
from app.models.user import User
from app.models.interview import InterviewResult as DBInterviewResult, InterviewSession
from app.database import get_db, async_session
from app.utils.limiter import limiter
from app.utils.file import extract_text_from_pdf, validate_file_security, ALLOWED_AUDIO_EXTENSIONS, validate_media_duration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["Audio Interview"])
audio_interview_service = AudioInterviewService()

TEMP_DIR = "temp_audios"
os.makedirs(TEMP_DIR, exist_ok=True)

@router.get("/session/{session_id}")
@limiter.limit("10/minute")
async def get_audio_session_status(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches the current state of an active audio interview session."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    return {
        "session_id": session.id,
        "questions": session.questions,
        "total_questions": len(session.questions),
        "current_question_index": session.question_number,
        "history": session.history,
        "warnings_count": session.warnings_count,
        "is_active": session.is_active
    }


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
        if not await can_use_feature_async(db, current_user.id, "audio_interview"):
            raise HTTPException(status_code=403, detail="Audio interview limit reached. Please upgrade or purchase one.")

        validate_job_description(job_description)

        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)
        req = InterviewStartRequest(resume_text=resume_text, job_description=job_description)

        response = await audio_interview_service.start_audio_interview(db, current_user.id, req)

        # Deduct usage only if successful
        await deduct_feature_usage_atomic(db, current_user.id, "audio_interview")
        await db.commit()

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting audio interview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{session_id}/answer")
async def save_audio_answer(
    session_id: str,
    background_tasks: BackgroundTasks,
    question_index: int = Form(...),
    audio: UploadFile = File(...),
    retaken: bool = Form(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Receives a single question's audio and processes it in the background."""
    ext = os.path.splitext(audio.filename or "")[1] or ".webm"
    safe_ext = ext if ext.lower() in {".webm", ".mp3", ".wav", ".m4a", ".ogg"} else ".webm"
    temp_path = os.path.join(TEMP_DIR, f"ans_{session_id}_{question_index}_{uuid.uuid4().hex[:6]}{safe_ext}")

    try:
        validate_file_security(audio, ALLOWED_AUDIO_EXTENSIONS, max_size_mb=5)
        
        content = await audio.read()
        async with aiofiles.open(temp_path, "wb") as out_file:
            await out_file.write(content)

        # Enforce audio duration safety validation (2 minutes + 10s buffer = 130s)
        validate_media_duration(temp_path, max_duration_seconds=130.0)

        # Update session progress and get context for STT
        context = await audio_interview_service.save_audio_answer(db, session_id, question_index, temp_path, retaken=retaken)
        
        # Trigger background STT
        background_tasks.add_task(
            audio_interview_service.process_stt_background,
            session_id=context["session_id"],
            question_index=context["question_index"],
            question_text=context["question_text"],
            audio_path=context["audio_path"],
            db_factory=async_session
        )
        
        return {"status": "processing", "message": "Audio received and processing started."}
        
    except ValueError as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Error saving audio answer: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_id}/result", response_model=InterviewResult)
@limiter.limit("10/minute")
async def get_audio_result_get(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches result from history or generates it if session still exists."""
    stmt = select(DBInterviewResult).where(
        DBInterviewResult.session_id == session_id,
        DBInterviewResult.user_id == current_user.id
    )
    result = await db.execute(stmt)
    db_result = result.scalar_one_or_none()
    
    if db_result:
        return db_result.result_data
        
    # Fallback to generation if session exists
    try:
        return await get_audio_result(request, session_id, current_user, db)
    except Exception:
        raise HTTPException(status_code=404, detail="Result not found")


@router.post("/{session_id}/result", response_model=InterviewResult)
@limiter.limit("5/minute")
async def get_audio_result(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Generates the final report based on accumulated transcripts."""
    try:
        # Check if result already exists
        existing_stmt = select(DBInterviewResult).where(
            DBInterviewResult.session_id == session_id,
            DBInterviewResult.user_id == current_user.id
        )
        existing_res = await db.execute(existing_stmt)
        if existing_db_res := existing_res.scalar_one_or_none():
            return existing_db_res.result_data

        # Get warnings count before deleting session
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        session_res = await db.execute(stmt)
        session = session_res.scalar_one_or_none()
        warnings_count = session.warnings_count if session else 0

        result_data = await audio_interview_service.generate_result(db, session_id)
        result_data["warnings_count"] = warnings_count # Include for record

        # Delete previous audio results for this user
        delete_stmt = delete(DBInterviewResult).where(
            DBInterviewResult.user_id == current_user.id,
            DBInterviewResult.interview_type == "audio"
        )
        await db.execute(delete_stmt)
        
        # Save to DB with session_id
        db_result = DBInterviewResult(
            user_id=current_user.id,
            session_id=session_id, # Persistent session ID for refresh
            interview_type="audio",
            result_data=result_data
        )
        db.add(db_result)
        
        # Cleanup: Delete the persistent session after successful result generation
        session_delete_stmt = delete(InterviewSession).where(InterviewSession.id == session_id)
        await db.execute(session_delete_stmt)
        
        await db.commit()
        
        return result_data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating audio result: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{session_id}/warning")
async def report_warning(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Increments the warning counter for a session."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    session.warnings_count = (session.warnings_count or 0) + 1
    
    if session.warnings_count >= 5:
        session.is_active = False
        # Optional: Log malpractice reason
        
    await db.commit()
    return {"warnings_count": session.warnings_count, "is_active": session.is_active}


@router.post("/{session_id}/malpractice")
async def report_malpractice(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Terminates a session due to malpractice."""
    stmt = update(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id
    ).values(is_active=False)
    
    await db.execute(stmt)
    await db.commit()
    return {"status": "terminated", "message": "Interview ended due to malpractice."}
