from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.services.interview_service import InterviewService
from app.utils.auth import get_current_user
from app.utils.usage import deduct_feature_usage_atomic, refund_feature_usage_atomic

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
from sqlalchemy.exc import IntegrityError
from app.utils.limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter()
interview_service = InterviewService()


def _latest_interview_history_item(record: DBInterviewResult | None, interview_type: str) -> dict:
    if not record:
        return {
            "created_at": None,
            "display_label": f"No {interview_type} interview yet",
        }

    return {
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "display_label": f"Latest {interview_type} interview",
    }


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
            "question_number": s.question_number,
            "processing_status": s.processing_status or "idle",
            "result_status": s.result_status or "pending",
            "warnings_count": s.warnings_count or 0,
            "is_active": s.is_active,
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
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "video"
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



from app.utils.file import extract_text_from_pdf, validate_file_security, ALLOWED_VIDEO_EXTENSIONS, validate_media_duration
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
        # Validate inputs before touching credits
        validate_job_description(job_description)
        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)

        # RESERVATION-BASED: deduct_feature_usage_atomic is the sole gatekeeper.
        # It acquires a SELECT FOR UPDATE row lock, checks credits, and deducts
        # in a single atomic step — no separate eligibility check, no TOCTOU gap.
        # Raises HTTP 403 automatically if credits are zero.
        credit_type = await deduct_feature_usage_atomic(db, current_user.id, "mock_interview")
        await db.commit()  # Commit reservation immediately so all concurrent requests see it

        from app.schemas.interview import InterviewStartRequest
        req = InterviewStartRequest(resume_text=resume_text, job_description=job_description)

        try:
            response = await interview_service.start_interview(db, current_user.id, req)
            await db.commit()
            return response
        except Exception as ai_err:
            # AI call failed — refund the reserved credit
            logger.error(f"AI call failed after credit reservation, refunding: {ai_err}")
            await db.rollback()
            await refund_feature_usage_atomic(db, current_user.id, "mock_interview", credit_type)
            await db.commit()
            if isinstance(ai_err, IntegrityError):
                raise HTTPException(
                    status_code=400,
                    detail="You already have an active video interview session. Please complete or resume it before starting a new one."
                )
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error starting interview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not start interview. Please try again.")


import aiofiles


@router.post("/{session_id}/response", response_model=InterviewResponse)
@limiter.limit("10/minute")
async def process_response(
    request: Request,
    session_id: str,
    video: UploadFile = File(...),
    retaken: bool = Form(default=False),
    # Fix #2: require authenticated user so sessions cannot be hijacked
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Ownership and existence check
    session_stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "video",
        InterviewSession.is_active == True
    )
    session_res = await db.execute(session_stmt)
    session = session_res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found, unauthorized, or already completed")

    # Fix #8: use a server-generated UUID filename, never trust client-supplied filenames
    ext = os.path.splitext(video.filename or "")[1] or ".webm"
    safe_ext = ext if ext.lower() in {".webm", ".mp4", ".ogg", ".mkv"} else ".webm"
    temp_path = os.path.join(TEMP_DIR, f"{uuid.uuid4()}{safe_ext}")

    try:
        # Enforce security validation
        validate_file_security(video, ALLOWED_VIDEO_EXTENSIONS, max_size_mb=50)

        content = await video.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="No video answer was recorded. Please record an answer before submitting.")

        async with aiofiles.open(temp_path, "wb") as out_file:
            await out_file.write(content)

        logger.info(f"Saved video to {temp_path}")
        
        # Enforce video duration safety validation (2 minutes 30 seconds + 10s buffer = 160s)
        validate_media_duration(temp_path, max_duration_seconds=160.0)
        
        response = await interview_service.process_response(db, session_id, temp_path)
        await db.commit() # Commit history updates

        return response
    except ValueError as e:
        logger.warning("Invalid interview response for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail="Invalid interview response. Please try again.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing response: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not process interview response. Please try again.")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/{session_id}/skip", response_model=InterviewResponse)
@limiter.limit("10/minute")
async def skip_response(
    request: Request,
    session_id: str,
    reason: str = Form(default="Candidate failed to answer this question within the time limit."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    session_stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "video",
        InterviewSession.is_active == True
    )
    session_res = await db.execute(session_stmt)
    session = session_res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found, unauthorized, or already completed")

    try:
        response = await interview_service.skip_response(db, session_id, reason)
        await db.commit()
        return response
    except ValueError as e:
        logger.warning("Invalid video skip for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail="Could not skip this question.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error skipping video question: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not skip this question. Please try again.")


@router.post("/{session_id}/resume-grace", response_model=InterviewResponse)
@limiter.limit("5/minute")
async def grant_resume_grace(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    session_stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "video",
        InterviewSession.is_active == True
    )
    session_res = await db.execute(session_stmt)
    session = session_res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found, unauthorized, or already completed")

    try:
        response = await interview_service.grant_resume_grace(db, session_id)
        await db.commit()
        return response
    except ValueError as e:
        logger.warning("Invalid resume grace request for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail="Could not resume this question.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error granting resume grace: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not resume this question. Please try again.")

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
        
        latest_result = None
        if video_result and audio_result:
            latest_result = video_result if video_result.created_at >= audio_result.created_at else audio_result
        else:
            latest_result = video_result or audio_result

        latest_type = latest_result.interview_type if latest_result else None
        latest_created_at = latest_result.created_at.isoformat() if latest_result and latest_result.created_at else None

        return {
            "video": video_result.result_data if video_result else None,
            "audio": audio_result.result_data if audio_result else None,
            "video_meta": _latest_interview_history_item(video_result, "video"),
            "audio_meta": _latest_interview_history_item(audio_result, "audio"),
            "latest": {
                "type": latest_type,
                "created_at": latest_created_at,
                "display_label": f"Latest {latest_type} interview" if latest_type else "No interview yet",
            },
        }
    except Exception as e:
        logger.error("Error fetching history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not fetch interview history. Please try again.")



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

        # Lock and mark the session so duplicate requests cannot generate twice.
        session_stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.user_id == current_user.id,
            InterviewSession.interview_type == "video"
        ).with_for_update()
        session_res = await db.execute(session_stmt)
        session = session_res.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found or unauthorized")

        if session.result_status == "generating":
            raise HTTPException(status_code=202, detail="Result generation is already in progress. Please try again shortly.")

        if (session.warnings_count or 0) >= 5:
            raise HTTPException(status_code=400, detail="Interview discarded due to malpractice. No result generated.")

        if len(session.history or []) < 8:
            raise HTTPException(
                status_code=400,
                detail=f"Interview is incomplete. Please complete all questions (currently at {len(session.history or [])}/8) before requesting the result."
            )

        # Block result generation until the interview is actually complete
        if session.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"Interview is still in progress. Please complete all questions (currently at {len(session.history or [])}/8) before requesting the result."
            )

        session.result_status = "generating"
        await db.commit()

        try:
            result_data = await interview_service.generate_result(db, session_id)
        except Exception:
            status_stmt = select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
            status_res = await db.execute(status_stmt)
            status_session = status_res.scalar_one_or_none()
            if status_session:
                status_session.result_status = "failed"
                await db.commit()
            raise

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
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Invalid result request for session %s: %s", session_id, e)
        raise HTTPException(status_code=404, detail="Interview result is not available.")
    except Exception as e:
        logger.error("Error generating result: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not generate interview result. Please try again.")


@router.post("/{session_id}/warning")
async def report_warning(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Increments the warning counter for a session."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "video"
    ).with_for_update()
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    session.warnings_count = (session.warnings_count or 0) + 1
    
    if session.warnings_count >= 5:
        session.is_active = False
        
    await db.commit()
    return {"warnings_count": session.warnings_count, "is_active": session.is_active}


@router.post("/{session_id}/malpractice")
async def report_malpractice(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Terminates a session due to malpractice."""
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == current_user.id,
        InterviewSession.interview_type == "video"
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    session.is_active = False
    session.warnings_count = max(session.warnings_count or 0, 5)
    await db.commit()
    return {
        "status": "terminated",
        "message": "Interview ended due to malpractice.",
        "warnings_count": session.warnings_count,
        "is_active": session.is_active,
    }
