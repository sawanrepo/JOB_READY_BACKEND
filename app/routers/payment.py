from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.models.subscription import SubscriptionPlan
from app.routers.auth import get_current_user_optional
import razorpay
import hmac
import hashlib
import os
from datetime import datetime, timedelta, timezone

router = APIRouter()

RAZORPAY_SECRET = os.getenv("RAZORPAY_SECRET")

@router.post("/payment/verify")
async def verify_payment(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    payload = await request.json()
    razorpay_order_id = payload.get("razorpay_order_id")
    razorpay_payment_id = payload.get("razorpay_payment_id")
    razorpay_signature = payload.get("razorpay_signature")
    user_id = payload.get("user_id")
    plan_name = payload.get("plan_name")

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, user_id, plan_name]):
        raise HTTPException(status_code=400, detail="Missing payment data")

    # Verify signature
    generated_signature = hmac.new(
        bytes(RAZORPAY_SECRET, "utf-8"),
        bytes(f"{razorpay_order_id}|{razorpay_payment_id}", "utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if generated_signature != razorpay_signature:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Update user subscription
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    plan = await db.execute(
        SubscriptionPlan.__table__.select().where(SubscriptionPlan.name == plan_name)
    )
    plan = plan.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if user.subscription_id != 1:
        return JSONResponse(content={"message": "User already has an active plan."})

    user.subscription_id = plan.id
    user.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    user.ats_checks_left_today = plan.max_ats_checks
    user.resume_tailoring_left_this_week = plan.max_resume_tailoring
    user.mock_interviews_left_this_month = plan.max_mock_interviews
    await db.commit()

    return JSONResponse(content={"message": "Subscription activated"})