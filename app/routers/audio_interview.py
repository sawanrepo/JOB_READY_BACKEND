from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form, Request, BackgroundTasks
import os
import uuid
import logging
import aiofiles
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.services.audio_interview_service import AudioInterviewService
from app.utils.auth import get_current_user
from app.utils.usage import deduct_feature_usage_atomic, refund_feature_usage_atomic
from app.utils.validation import validate_job_description, validate_resume_text
from app.models.user import User
from app.models.interview import InterviewResult as DBInterviewResult, InterviewSession
from app.database import get_db, async_session
from app.utils.limiter import limiter
from app.utils.file import extract_text_from_pdf, validate_file_security, ALLOWED_AUDIO_EXTENSIONS, validate_media_duration
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["Audio Interview"])
audio_interview_service = AudioInterviewService()

TEMP_DIR = "temp_audios"
os.makedirs(TEMP_DIR, exist_ok=True)

AUDIO_EXTENSIONS_BY_MIME = {
    "audio/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
}

UNSUPPORTED_AUDIO_FORMAT_MESSAGE = (
    "Unsupported audio format. Please use a supported browser that records WebM audio, "
    "such as the latest Google Chrome or Microsoft Edge."
)


def _extension_from_upload(audio: UploadFile) -> str:
    content_type = (audio.content_type or "").split(";")[0].lower()
    if content_type in AUDIO_EXTENSIONS_BY_MIME:
        return AUDIO_EXTENSIONS_BY_MIME[content_type]

    ext = os.path.splitext(audio.filename or "")[1].lower()
    if ext in ALLOWED_AUDIO_EXTENSIONS:
        return ext

    raise HTTPException(status_code=400, detail=UNSUPPORTED_AUDIO_FORMAT_MESSAGE)


def _audio_transcription_progress(session: InterviewSession) -> dict:
    questions = session.questions or []
    history = session.history or []
    transcribed_count = sum(
        1
        for item in history
        if item is not None and not item.get("error") and item.get("answer_text")
    )
    total_questions = len(questions)
    current_answer_index = 0
    for index in range(total_questions):
        item = history[index] if index < len(history) else None
        if item is None or item.get("error") or not item.get("answer_text"):
            current_answer_index = index + 1
            break
    if total_questions and current_answer_index == 0:
        current_answer_index = total_questions

    return {
        "stage": "transcribing",
        "message": "Interview answers are still being transcribed.",
        "transcribed_count": transcribed_count,
        "total_questions": total_questions,
        "current_answer_index": current_answer_index,
    }


@router.get("/session/{session_id}")
@limiter.limit("30/minute")
async def get_audio_session_status(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches the current state of an active audio interview session."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "audio"
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
        "is_active": session.is_active,
        "processing_status": session.processing_status or "idle",
        "result_status": session.result_status or "pending",
        "transcription_progress": _audio_transcription_progress(session),
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
    user_id = current_user.id
    try:
        if not audio_interview_service.s3 or not audio_interview_service.transcribe or not settings.AWS_S3_BUCKET_NAME:
            raise HTTPException(status_code=503, detail="Audio transcription service is not configured. Please try again later.")

        # Validate inputs before touching credits
        validate_job_description(job_description)
        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)

        # RESERVATION-BASED: deduct_feature_usage_atomic is the sole gatekeeper.
        # It acquires a SELECT FOR UPDATE row lock, checks credits, and deducts
        # in a single atomic step — no separate eligibility check, no TOCTOU gap.
        # Raises HTTP 403 automatically if credits are zero.
        credit_type = await deduct_feature_usage_atomic(db, user_id, "audio_interview")
        await db.commit()  # Commit reservation immediately so concurrent requests see it

        req = InterviewStartRequest(resume_text=resume_text, job_description=job_description)

        try:
            response = await audio_interview_service.start_audio_interview(db, user_id, req)
            await db.commit()
            return response
        except Exception as ai_err:
            # AI/session creation failed — refund the reserved credit
            logger.error(f"Audio interview start failed after credit reservation, refunding: {ai_err}")
            await db.rollback()
            await refund_feature_usage_atomic(db, user_id, "audio_interview", credit_type)
            await db.commit()
            if isinstance(ai_err, IntegrityError):
                raise HTTPException(
                    status_code=400,
                    detail="You already have an active audio interview session. Please complete or resume it before starting a new one."
                )
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error starting audio interview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not start audio interview. Please try again.")


@router.post("/{session_id}/answer")
@limiter.limit("10/minute")
async def save_audio_answer(
    request: Request,
    session_id: str,
    background_tasks: BackgroundTasks,
    question_index: int = Form(...),
    audio: UploadFile = File(...),
    retaken: bool = Form(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Ownership and existence check
    session_stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "audio",
        InterviewSession.is_active == True
    )
    session_res = await db.execute(session_stmt)
    session = session_res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found, unauthorized, or already completed")

    if not audio_interview_service.s3 or not audio_interview_service.transcribe or not settings.AWS_S3_BUCKET_NAME:
        raise HTTPException(status_code=503, detail="Audio transcription service is not configured. Please try again later.")

    safe_ext = _extension_from_upload(audio)
    temp_path = os.path.join(TEMP_DIR, f"ans_{session_id}_{question_index}_{uuid.uuid4().hex[:6]}{safe_ext}")

    try:
        validate_file_security(audio, ALLOWED_AUDIO_EXTENSIONS, max_size_mb=5)
        
        content = await audio.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="No audio answer was recorded. Please record an answer before submitting.")

        async with aiofiles.open(temp_path, "wb") as out_file:
            await out_file.write(content)

        # Enforce audio duration safety validation (2 minutes + 10s buffer = 130s)
        validate_media_duration(temp_path, max_duration_seconds=130.0)

        # Update session progress and get context for STT
        context = await audio_interview_service.save_audio_answer(db, session_id, question_index, temp_path)
        if context.get("duplicate"):
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return {
                "status": "duplicate",
                "message": "Audio answer was already received.",
                "current_question_index": context.get("current_question_index"),
            }
        if context.get("timed_out"):
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return {
                "status": "timed_out",
                "message": "This question timed out and the interview moved to the next question.",
                "current_question_index": context.get("current_question_index"),
            }
        
        # Trigger background STT
        background_tasks.add_task(
            audio_interview_service.process_stt_background,
            session_id=context["session_id"],
            question_index=context["question_index"],
            question_text=context["question_text"],
            audio_path=context["audio_path"],
            db_factory=async_session
        )
        
        return {
            "status": "processing",
            "message": "Audio received and processing started.",
            "current_question_index": context.get("current_question_index"),
        }
        
    except ValueError as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.warning("Invalid audio answer for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail="Invalid audio answer. Please try again.")
    except HTTPException as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error("Error saving audio answer: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not save audio answer. Please try again.")


@router.post("/{session_id}/skip")
@limiter.limit("10/minute")
async def skip_audio_answer(
    request: Request,
    session_id: str,
    question_index: int = Form(...),
    reason: str = Form(default="Candidate failed to answer this question within the time limit."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    session_stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "audio",
        InterviewSession.is_active == True
    )
    session_res = await db.execute(session_stmt)
    session = session_res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found, unauthorized, or already completed")

    try:
        context = await audio_interview_service.skip_audio_answer(db, session_id, question_index, reason)
        return {"status": "skipped", **context}
    except ValueError as e:
        logger.warning("Invalid audio skip for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail="Could not skip this question.")
    except Exception as e:
        logger.error("Error skipping audio answer: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not skip this question. Please try again.")


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
    except HTTPException as e:
        raise e
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

        # Lock and mark the session so duplicate requests cannot generate twice.
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.user_id == current_user.id,
            InterviewSession.interview_type == "audio"
        ).with_for_update()
        session_res = await db.execute(stmt)
        session = session_res.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found or unauthorized")

        if session.result_status == "generating":
            raise HTTPException(
                status_code=202,
                detail={
                    "stage": "generating_report",
                    "message": "Generating final report. Please try again shortly.",
                },
            )

        # Block result generation until all questions have been answered
        if session.question_number < len(session.questions or []):
            raise HTTPException(
                status_code=400,
                detail=f"Interview is still in progress. Please complete all questions (currently at {session.question_number}/{len(session.questions or [])}) before requesting the result."
            )
        history = session.history or []
        questions = session.questions or []
        transcripts_complete = (
            len(history) == len(questions)
            and all(
                item is not None and not item.get("error") and item.get("answer_text")
                for item in history
            )
        )
        if not transcripts_complete:
            raise HTTPException(status_code=202, detail=_audio_transcription_progress(session))

        warnings_count = session.warnings_count if session else 0
        session.result_status = "generating"
        await db.commit()

        try:
            result_data = await audio_interview_service.generate_result(db, session_id)
            result_data["warnings_count"] = warnings_count # Include for record
        except Exception:
            status_stmt = select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
            status_res = await db.execute(status_stmt)
            status_session = status_res.scalar_one_or_none()
            if status_session:
                status_session.result_status = "failed"
                await db.commit()
            raise

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
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Invalid audio result request for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail="Audio result is not available yet.")
    except Exception as e:
        logger.error("Error generating audio result: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not generate audio result. Please try again.")


@router.post("/{session_id}/warning")
@limiter.limit("10/minute")
async def report_warning(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Increments the warning counter for a session."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "audio"
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
@limiter.limit("5/minute")
async def report_malpractice(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Terminates a session due to malpractice."""
    stmt = update(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "audio"
    ).values(is_active=False, warnings_count=5)
    
    await db.execute(stmt)
    await db.commit()
    return {"status": "terminated", "message": "Interview ended due to malpractice."}
