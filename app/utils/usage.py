from datetime import datetime, timezone
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from fastapi import HTTPException

async def can_use_feature_async(db: AsyncSession, user_id: int, feature: str) -> bool:
    """
    Checks if a user can use a feature by querying the latest data from DB.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return False
        
    mapping = {
        "ats_check": (user.ats_checks_left_today or 0) + (user.purchased_ats_credits or 0),
        "resume_tailoring": (user.resume_tailoring_left_this_week or 0) + (user.purchased_tailor_credits or 0),
        "mock_interview": user.mock_interviews_left or 0,
        "audio_interview": user.audio_interviews_left or 0
    }
    return mapping.get(feature, 0) > 0


async def deduct_feature_usage_atomic(db: AsyncSession, user_id: int, feature: str) -> str:
    """
    Deducts feature usage using a row-level lock to prevent race conditions.
    Raises HTTPException if credits are insufficient.
    """
    # 1. Lock the user row for update
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    
    credit_type = "daily"
    
    if feature == "ats_check":
        if (user.ats_checks_left_today or 0) > 0:
            user.ats_checks_left_today -= 1
            credit_type = "daily"
        elif (user.purchased_ats_credits or 0) > 0:
            user.purchased_ats_credits -= 1
            credit_type = "purchased"
        else:
            raise HTTPException(status_code=403, detail="Insufficient ATS check credits.")
        user.last_ats_check_at = now

    elif feature == "resume_tailoring":
        if (user.resume_tailoring_left_this_week or 0) > 0:
            user.resume_tailoring_left_this_week -= 1
            credit_type = "weekly"
        elif (user.purchased_tailor_credits or 0) > 0:
            user.purchased_tailor_credits -= 1
            credit_type = "purchased"
        else:
            raise HTTPException(status_code=403, detail="Insufficient resume tailoring credits.")
        user.last_resume_tailoring_at = now

    elif feature == "mock_interview":
        if (user.mock_interviews_left or 0) > 0:
            user.mock_interviews_left -= 1
            credit_type = "purchased"
        else:
            raise HTTPException(status_code=403, detail="Insufficient mock interview credits.")
        user.last_mock_interview_at = now

    elif feature == "audio_interview":
        if (user.audio_interviews_left or 0) > 0:
            user.audio_interviews_left -= 1
            credit_type = "purchased"
        else:
            raise HTTPException(status_code=403, detail="Insufficient audio interview credits.")
        user.last_audio_interview_at = now

    # The changes are applied to the 'user' object which is tracked by the session.
    # They will be committed when db.commit() is called in the router.
    return credit_type


async def refund_feature_usage_atomic(db: AsyncSession, user_id: int, feature: str, credit_type: str = "daily"):
    """
    Refunds a credit to the user if a task failed after deduction.
    Uses row-level locking for safety.
    """
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        return 

    if feature == "ats_check":
        if credit_type == "purchased":
            user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 1
        else:
            user.ats_checks_left_today = (user.ats_checks_left_today or 0) + 1
        
    elif feature == "resume_tailoring":
        if credit_type == "purchased":
            user.purchased_tailor_credits = (user.purchased_tailor_credits or 0) + 1
        else:
            user.resume_tailoring_left_this_week = (user.resume_tailoring_left_this_week or 0) + 1
        
    elif feature == "mock_interview":
        user.mock_interviews_left = (user.mock_interviews_left or 0) + 1
        
    elif feature == "audio_interview":
        user.audio_interviews_left = (user.audio_interviews_left or 0) + 1