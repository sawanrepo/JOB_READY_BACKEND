from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.models.user import User
from app.models.subscription import SubscriptionPlan
from app.routers.auth import get_current_user

router = APIRouter()

@router.post("/subscribe/{plan_name}")
async def subscribe(plan_name: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    #Block if already subscribed and not expired
    if current_user.subscription_id != 1 and current_user.subscription_expires_at and current_user.subscription_expires_at > datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Active subscription already exists.")

    plan = await db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.name == plan_name)
    )
    plan = plan.scalars().first()

    if not plan:
        raise HTTPException(status_code=404, detail="Subscription plan not found")

    current_user.subscription_id = plan.id
    current_user.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    # Reset usage based on new plan
    current_user.ats_checks_left_today = plan.max_ats_checks
    current_user.resume_tailoring_left_this_week = plan.max_resume_tailoring
    current_user.mock_interviews_left = (current_user.mock_interviews_left or 0) + plan.max_mock_interviews


    await db.commit()
    return {"message": f"Successfully subscribed to {plan_name}"}