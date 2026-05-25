from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from app.schemas.resume import ResumeAnalysisResponse, TailoredResumeResponse, TailoredContent
from app.services.resume import run_analyze_resume, tailor_resume, propose_tailor_resume, generate_tailored_pdf
from app.utils.auth import get_current_user
from app.models.user import User
from app.utils.file import extract_text_from_pdf
import os
import logging
import asyncio
from pathlib import Path
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, desc
from app.database import get_db
from app.models.resume import ResumeHistory as DBResumeHistory
from app.utils.usage import (
    FREE_ATS_CHECKS_PER_DAY,
    FREE_RESUME_TAILORING_PER_WEEK,
    deduct_feature_usage_atomic,
    refund_feature_usage_atomic,
)
from app.utils.s3 import upload_file_to_s3, delete_file_from_s3, get_file_from_s3

logger = logging.getLogger(__name__)


from app.utils.validation import (
    get_job_input_note,
    get_job_input_type,
    validate_job_target,
    validate_resume_text,
)

from datetime import datetime, timedelta, timezone

from app.utils.limiter import limiter

router = APIRouter()
OUTPUT_DIR = Path("output").resolve()


def _attach_job_input_metadata(payload: dict, job_input: str) -> dict:
    payload["job_input_type"] = get_job_input_type(job_input)
    note = get_job_input_note(job_input)
    if note:
        payload["result_note"] = note
    return payload


def _delete_legacy_local_resume(file_path: str | None, context: str) -> None:
    if not file_path or file_path.startswith("tailored_resumes/"):
        return

    old_file_path = (OUTPUT_DIR / file_path).resolve()
    try:
        old_file_path.relative_to(OUTPUT_DIR)
    except ValueError:
        logger.warning("Skipping unsafe legacy resume cleanup path during %s: %s", context, file_path)
        return

    if old_file_path.exists():
        try:
            os.remove(old_file_path)
        except Exception as e:
            logger.error("Error deleting old local resume file during %s: %s", context, e)


def _delete_local_file(file_path: str | None, context: str) -> None:
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
        logger.info("Successfully deleted temporary local file during %s: %s", context, file_path)
    except Exception as e:
        logger.error("Failed to delete temporary local file during %s: %s", context, e)


@router.post("/ats-check", response_model=ResumeAnalysisResponse)
@limiter.limit("5/minute")
async def ats_check(
    request: Request,
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    user_id = current_user.id
    try:
        # 1. Extract and Validate inputs first (before reserving credits)
        validate_job_target(job_description)
        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)

        # RESERVATION-BASED: deduct_feature_usage_atomic is the sole gatekeeper.
        # It acquires a SELECT FOR UPDATE row lock, checks credits, and deducts
        # in a single atomic step — no separate eligibility check, no TOCTOU gap.
        # Raises HTTP 403 automatically if credits are zero.
        credit_type = await deduct_feature_usage_atomic(db, user_id, "ats_check")
        await db.commit()  # Save reservation immediately so concurrent requests see it

        try:
            # 2. AI Analysis
            result_dict = await run_analyze_resume(resume_text, job_description)
            _attach_job_input_metadata(result_dict, job_description)
            
            # Check if AI identified it as a non-resume
            if not result_dict.get("is_resume", True):
                raise HTTPException(status_code=400, detail=result_dict.get("error_message", "The uploaded PDF does not appear to be a valid resume."))

            result = ResumeAnalysisResponse(**result_dict)
        except Exception as ai_err:
            # AI failed or invalid resume — refund the reserved credit
            logger.error(f"ATS Analysis failed after reservation, refunding: {ai_err}")
            await refund_feature_usage_atomic(db, user_id, "ats_check", credit_type)
            await db.commit()
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("ATS Analysis failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="ATS analysis failed. Please try again.")



    # History update happens after successful analysis


    
    # Update History: Delete old ATS and save new one
    delete_stmt = delete(DBResumeHistory).where(
        DBResumeHistory.user_id == user_id,
        DBResumeHistory.history_type == "ats"
    )
    await db.execute(delete_stmt)
    
    new_history = DBResumeHistory(
        user_id=user_id,
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
    user_id = current_user.id
    try:
        # 1. Extract and Validate inputs first (before reserving credits)
        validate_job_target(job_description)
        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)
        await resume_pdf.seek(0)

        # RESERVATION-BASED: deduct_feature_usage_atomic is the sole gatekeeper.
        # It acquires a SELECT FOR UPDATE row lock, checks credits, and deducts
        # in a single atomic step — no separate eligibility check, no TOCTOU gap.
        # Raises HTTP 403 automatically if credits are zero.
        credit_type = await deduct_feature_usage_atomic(db, user_id, "resume_tailoring")
        await db.commit()  # Save reservation immediately so concurrent requests see it

        try:
            # 2. AI Tailoring
            result_dict = await tailor_resume(resume_pdf, job_description)
            _attach_job_input_metadata(result_dict, job_description)
            if isinstance(result_dict.get("tailored_content"), dict):
                _attach_job_input_metadata(result_dict["tailored_content"], job_description)
            
            # Check if AI identified it as a non-resume
            if not result_dict.get("is_resume", True):
                raise HTTPException(status_code=400, detail=result_dict.get("error_message", "The uploaded PDF does not appear to be a valid resume."))

            result = TailoredResumeResponse(**result_dict)
        except Exception as ai_err:
            # AI failed or invalid resume — refund the reserved credit
            logger.error(f"Resume tailoring failed after reservation, refunding: {ai_err}")
            await refund_feature_usage_atomic(db, user_id, "resume_tailoring", credit_type)
            await db.commit()
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Resume tailoring failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Resume tailoring failed. Please try again.")

    # History update happens after successful tailoring


    
    local_pdf_path = str(OUTPUT_DIR / result.filename) if result.filename else None
    try:
        if not local_pdf_path:
            raise HTTPException(status_code=500, detail="Failed to generate tailored resume PDF locally.")

        old_tailor_stmt = select(DBResumeHistory).where(
            DBResumeHistory.user_id == user_id,
            DBResumeHistory.history_type == "tailor"
        )
        old_tailor = (await db.execute(old_tailor_stmt)).scalar_one_or_none()

        s3_key = f"tailored_resumes/{user_id}/resume.pdf"
        upload_success = await upload_file_to_s3(local_pdf_path, s3_key)
        if not upload_success:
            raise HTTPException(status_code=500, detail="Failed to upload tailored resume to secure S3 storage.")

        delete_stmt = delete(DBResumeHistory).where(
            DBResumeHistory.user_id == user_id,
            DBResumeHistory.history_type == "tailor"
        )
        await db.execute(delete_stmt)

        new_history = DBResumeHistory(
            user_id=user_id,
            history_type="tailor",
            result_data={"filename": "resume.pdf", "s3_key": s3_key, "editable_draft": False},
            file_path=s3_key
        )
        db.add(new_history)
        await db.commit()

        _delete_legacy_local_resume(old_tailor.file_path if old_tailor else None, "tailor")
    except Exception as storage_err:
        await db.rollback()
        logger.error("Resume tailoring storage failed after reservation, refunding: %s", storage_err)
        await refund_feature_usage_atomic(db, user_id, "resume_tailoring", credit_type)
        await db.commit()
        _delete_local_file(local_pdf_path, "tailor storage failure")
        if isinstance(storage_err, HTTPException):
            raise storage_err
        raise HTTPException(status_code=500, detail="Failed to store tailored resume securely.")

    _delete_local_file(local_pdf_path, "tailor")

    # Update result pdf_url to point to /download/resume.pdf so download routing is consistent
    result.pdf_url = "/download/resume.pdf"
    result.filename = "resume.pdf"
    return result

@router.post("/tailor-resume/propose", response_model=TailoredContent)
@limiter.limit("5/minute")
async def tailor_resume_propose_endpoint(
    request: Request,
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    user_id = current_user.id
    try:
        # 1. Extract and Validate inputs first (before reserving credits)
        validate_job_target(job_description)
        resume_text = await extract_text_from_pdf(resume_pdf)
        validate_resume_text(resume_text)

        # RESERVATION-BASED: deduct_feature_usage_atomic is the sole gatekeeper.
        # It acquires a SELECT FOR UPDATE row lock, checks credits, and deducts
        # in a single atomic step — no separate eligibility check, no TOCTOU gap.
        # Raises HTTP 403 automatically if credits are zero.
        credit_type = await deduct_feature_usage_atomic(db, user_id, "resume_tailoring")
        await db.commit()  # Save reservation immediately so concurrent requests see it

        try:
            # 2. AI Proposing
            tailored_content = await propose_tailor_resume(resume_text, job_description)
            _attach_job_input_metadata(tailored_content, job_description)
            
            # Check if AI identified it as a non-resume
            if not tailored_content.get("is_resume", True):
                raise HTTPException(status_code=400, detail=tailored_content.get("error_message", "The uploaded PDF does not appear to be a valid resume."))

            # Generate a default PDF and save a one-time editable draft marker.
            local_pdf_path = None
            try:
                result_dict = await generate_tailored_pdf(tailored_content)

                old_tailor_stmt = select(DBResumeHistory).where(
                    DBResumeHistory.user_id == user_id,
                    DBResumeHistory.history_type == "tailor"
                )
                old_tailor = (await db.execute(old_tailor_stmt)).scalar_one_or_none()

                s3_key = f"tailored_resumes/{user_id}/resume.pdf"
                if not result_dict.get("filename"):
                    raise RuntimeError("Failed to generate default tailored resume PDF locally.")

                local_pdf_path = str(OUTPUT_DIR / result_dict["filename"])
                upload_success = await upload_file_to_s3(local_pdf_path, s3_key)
                if not upload_success:
                    raise RuntimeError("Failed to upload tailored resume proposal to secure S3 storage.")

                delete_stmt = delete(DBResumeHistory).where(
                    DBResumeHistory.user_id == user_id,
                    DBResumeHistory.history_type == "tailor"
                )
                await db.execute(delete_stmt)

                new_history = DBResumeHistory(
                    user_id=user_id,
                    history_type="tailor",
                    result_data={"filename": "resume.pdf", "s3_key": s3_key, "editable_draft": True},
                    file_path=s3_key
                )
                db.add(new_history)
                await db.commit()

                _delete_legacy_local_resume(old_tailor.file_path if old_tailor else None, "propose")
                _delete_local_file(local_pdf_path, "propose")
            except Exception:
                await db.rollback()
                _delete_local_file(local_pdf_path, "propose failure")
                raise

            return tailored_content
        except Exception as ai_err:
            # AI failed or invalid resume — refund the reserved credit
            logger.error(f"Resume tailoring proposal failed after reservation, refunding: {ai_err}")
            await refund_feature_usage_atomic(db, user_id, "resume_tailoring", credit_type)
            await db.commit()
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Resume tailoring proposal failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Resume tailoring proposal failed. Please try again.")

@router.post("/tailor-resume/generate", response_model=TailoredResumeResponse)
@limiter.limit("5/minute")
async def tailor_resume_generate_endpoint(
    request: Request,
    tailored_content: TailoredContent,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    user_id = current_user.id
    old_tailor_stmt = select(DBResumeHistory).where(
        DBResumeHistory.user_id == user_id,
        DBResumeHistory.history_type == "tailor"
    )
    old_tailor = (await db.execute(old_tailor_stmt)).scalar_one_or_none()
    draft_meta = old_tailor.result_data if old_tailor and isinstance(old_tailor.result_data, dict) else {}
    if not old_tailor or not draft_meta.get("editable_draft"):
        raise HTTPException(status_code=403, detail="Please start resume tailoring before generating an edited PDF.")

    try:
        content_dict = tailored_content.model_dump()
        result_dict = await generate_tailored_pdf(content_dict)
        
        result = TailoredResumeResponse(
            tailored_content=tailored_content,
            filename=result_dict["filename"],
            pdf_url=result_dict["pdf_url"],
            is_resume=True,
            job_input_type=content_dict.get("job_input_type"),
            result_note=content_dict.get("result_note")
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error("Resume tailoring compilation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Resume tailoring compilation failed. Please try again.")

    # Upload new tailored resume to S3
    s3_key = f"tailored_resumes/{user_id}/resume.pdf"
    if result.filename:
        local_pdf_path = str(OUTPUT_DIR / result.filename)
        upload_success = await upload_file_to_s3(local_pdf_path, s3_key)
        if not upload_success:
            # Clean up local PDF before raising error
            if os.path.exists(local_pdf_path):
                try:
                    os.remove(local_pdf_path)
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail="Failed to upload tailored resume to secure S3 storage.")
    else:
        raise HTTPException(status_code=500, detail="Failed to generate tailored resume PDF locally.")

    delete_stmt = delete(DBResumeHistory).where(
        DBResumeHistory.user_id == user_id,
        DBResumeHistory.history_type == "tailor"
    )
    await db.execute(delete_stmt)
    
    new_history = DBResumeHistory(
        user_id=user_id,
        history_type="tailor",
        result_data={"filename": "resume.pdf", "s3_key": s3_key, "editable_draft": False},
        file_path=s3_key
    )
    db.add(new_history)
    await db.commit()

    _delete_legacy_local_resume(old_tailor.file_path if old_tailor else None, "generate")

    # Delete local PDF after successful S3 upload
    if os.path.exists(local_pdf_path):
        try:
            os.remove(local_pdf_path)
            logger.info(f"Successfully deleted temporary local PDF: {local_pdf_path}")
        except Exception as e:
            logger.error(f"Failed to delete temporary local PDF: {e}")

    # Update result pdf_url to point to /download/resume.pdf so download routing is consistent
    result.pdf_url = "/download/resume.pdf"
    result.filename = "resume.pdf"
    return result

@router.get("/download/{filename}")
async def download_resume(
    filename: str,
    current_user: User = Depends(get_current_user),  # Fix #1: require authentication
    db: AsyncSession = Depends(get_db)
):
    # Add ownership check before resume file download to verify the file belongs to the user
    stmt = select(DBResumeHistory).where(
        DBResumeHistory.user_id == current_user.id,
        (DBResumeHistory.file_path == filename) | (DBResumeHistory.file_path == f"tailored_resumes/{current_user.id}/{filename}")
    )
    res = await db.execute(stmt)
    history = res.scalar_one_or_none()
    if not history:
        raise HTTPException(status_code=403, detail="Unauthorized to download this file")

    # Check expiration (30 days limit)
    now = datetime.now(timezone.utc)
    created_at = history.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
        
    if (now - created_at).days >= 30:
        # Proactively clean up only the file recorded on this user's history row.
        if history.file_path and history.file_path.startswith("tailored_resumes/"):
            await delete_file_from_s3(history.file_path)
        else:
            _delete_legacy_local_resume(history.file_path, "download expiry")
        raise HTTPException(
            status_code=400,
            detail="sorry cant display your tailored resume as we keep most recent tailored resume for 30 days only"
        )

    if history.file_path and history.file_path.startswith("tailored_resumes/"):
        # Stream through the authenticated backend route so frontend downloads do not depend on S3 CORS.
        s3_object = await get_file_from_s3(history.file_path)
        if not s3_object:
            raise HTTPException(status_code=404, detail="File not found")
        body = await asyncio.to_thread(s3_object["Body"].read)
        return Response(
            content=body,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    # Legacy local-file fallback only for histories that actually reference local files.
    safe_path = (OUTPUT_DIR / (history.file_path or "")).resolve()
    try:
        safe_path.relative_to(OUTPUT_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(safe_path), media_type="application/pdf", filename=os.path.basename(safe_path))

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
        
        tailor_filename = None
        tailor_message = None
        tailor_created_at = None
        
        if tailor_record:
            tailor_created_at = tailor_record.created_at.isoformat()
            now = datetime.now(timezone.utc)
            created_at = tailor_record.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
                
            if (now - created_at).days >= 30:
                tailor_filename = None
                tailor_message = "sorry cant display your tailored resume as we keep most recent tailored resume for 30 days only"
                # Proactively clean up S3 and local files
                if tailor_record.file_path:
                    if tailor_record.file_path.startswith("tailored_resumes/"):
                        await delete_file_from_s3(tailor_record.file_path)
                    else:
                        _delete_legacy_local_resume(tailor_record.file_path, "history expiry")
            else:
                if tailor_record.file_path.startswith("tailored_resumes/"):
                    tailor_filename = os.path.basename(tailor_record.file_path)
                else:
                    tailor_filename = tailor_record.file_path
        
        return {
            "ats": {
                "result": ats_record.result_data if ats_record else None,
                "created_at": ats_record.created_at.isoformat() if ats_record else None
            },
            "tailor": {
                "filename": tailor_filename,
                "created_at": tailor_created_at,
                "message": tailor_message
            }
        }
    except Exception as e:
        logger.error("Error fetching latest resume history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not fetch resume history. Please try again.")

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
        "ats_checks_left_today": min(current_user.ats_checks_left_today or 0, FREE_ATS_CHECKS_PER_DAY) + (current_user.purchased_ats_credits or 0),
        "resume_tailoring_left_this_week": min(current_user.resume_tailoring_left_this_week or 0, FREE_RESUME_TAILORING_PER_WEEK) + (current_user.purchased_tailor_credits or 0),
        "purchased_ats_credits": current_user.purchased_ats_credits or 0,
        "purchased_tailor_credits": current_user.purchased_tailor_credits or 0,
        "mock_interviews_left": current_user.mock_interviews_left or 0,
        "audio_interviews_left": current_user.audio_interviews_left or 0,
        "next_reset_times": {
            "ats_check": next_daily_reset.isoformat(),
            "resume_tailoring": next_weekly_reset.isoformat(),
        }
    }
