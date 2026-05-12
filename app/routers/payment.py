from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.models.subscription import SubscriptionPlan
import hmac
import hashlib
import logging
from datetime import datetime, timedelta, timezone
import razorpay
from app.routers.auth import get_current_user
from app.config import settings
from app.utils.logging import log_payment_event


logger = logging.getLogger(__name__)

router = APIRouter()

RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET = settings.RAZORPAY_KEY_SECRET

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

PLAN_PRICES = {
    "pro": {"amount": 21900, "plan_id": 2},               # ₹219
    "pro_plus": {"amount": 42900, "plan_id": 3},           # ₹429
    "ats_one_time": {"amount": 900, "plan_id": 0},         # ₹9
    "tailor_one_time": {"amount": 1900, "plan_id": 0},      # ₹19
    "audio_one_time": {"amount": 3900, "plan_id": 0},      # ₹39
    "video_one_time": {"amount": 8900, "plan_id": 0},      # ₹89
    "starter_pack": {"amount": 3900, "plan_id": 0},        # ₹39
    "resume_boost_pack": {"amount": 8900, "plan_id": 0},   # ₹89
    "interview_pack": {"amount": 13900, "plan_id": 0},     # ₹139
    "combo_pack": {"amount": 24900, "plan_id": 0},         # ₹249
}


@router.post("/create-order/{plan}")
async def create_order(plan: str, current_user: User = Depends(get_current_user)):
    if plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid plan")

    order = client.order.create({
        "amount": PLAN_PRICES[plan]["amount"],
        "currency": "INR",
        "payment_capture": 1,
        "notes": {"user_id": str(current_user.id)}
    })

    return {
        "order_id": order["id"],
        "key": RAZORPAY_KEY_ID,
        "amount": PLAN_PRICES[plan]["amount"],
        "plan": plan,
        "notes": order["notes"]
    }


@router.post("/payment/verify")
async def verify_payment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    payload = await request.json()
    razorpay_order_id = payload.get("razorpay_order_id")
    razorpay_payment_id = payload.get("razorpay_payment_id")
    razorpay_signature = payload.get("razorpay_signature")
    plan_name = payload.get("plan_name")

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, plan_name]):
        raise HTTPException(status_code=400, detail="Missing payment data")

    # Fix #6: use constant-time comparison to prevent HMAC timing attack
    msg = f"{razorpay_order_id}|{razorpay_payment_id}"
    generated_signature = hmac.new(
        bytes(RAZORPAY_KEY_SECRET, "utf-8"),
        bytes(msg, "utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(generated_signature, razorpay_signature):
        logger.warning("Payment signature mismatch for order %s", razorpay_order_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    user = current_user

    # Individual Packs
    if plan_name == "ats_one_time":
        user.purchased_ats_credits += 1
    elif plan_name == "tailor_one_time":
        user.purchased_tailor_credits += 1
    elif plan_name == "audio_one_time":
        user.audio_interviews_left += 1
    elif plan_name == "video_one_time":
        user.mock_interviews_left += 1
    
    # Bundles
    elif plan_name == "starter_pack":
        user.purchased_ats_credits += 3
        user.purchased_tailor_credits += 1
    elif plan_name == "resume_boost_pack":
        user.purchased_ats_credits += 5
        user.purchased_tailor_credits += 3
    elif plan_name == "interview_pack":
        user.audio_interviews_left += 2
        user.mock_interviews_left += 1
    elif plan_name == "combo_pack":
        user.purchased_ats_credits += 5
        user.purchased_tailor_credits += 5
        user.audio_interviews_left += 2
        user.mock_interviews_left += 1

    
    # Legacy plan names (for backward compatibility during transition if needed)
    elif plan_name == "one_time":
        user.purchased_ats_credits += 1
    elif plan_name == "mock_interview_one_time":
        user.mock_interviews_left += 1
    elif plan_name == "audio_interview_one_time":
        user.audio_interviews_left += 1
    
    else:
        # It's a subscription plan
        plan = await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.name == plan_name)
        )
        plan = plan.scalars().first()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        # Fix #13: also allow re-subscription if current plan is expired
        now = datetime.now(timezone.utc)
        plan_is_active = (
            user.subscription_id != 1
            and user.subscription_expires_at is not None
            and user.subscription_expires_at > now
        )
        if plan_is_active:
            return JSONResponse(content={"message": "User already has an active plan."})

        user.subscription_id = plan.id
        user.subscription_expires_at = now + timedelta(days=30)
        user.ats_checks_left_today = plan.max_ats_checks
        user.resume_tailoring_left_this_week = plan.max_resume_tailoring
        user.mock_interviews_left = (user.mock_interviews_left or 0) + plan.max_mock_interviews

    await db.commit()
    
    # Structured Payment Logging
    amount_paid = PLAN_PRICES.get(plan_name, {}).get("amount", 0) / 100.0 # Convert from paise to INR
    log_payment_event("PAYMENT_SUCCESS", str(user.id), amount_paid, f"PLAN: {plan_name}")
    
    logger.info("Payment processed for user %s: plan=%s", user.id, plan_name)
    return JSONResponse(content={"message": "Payment verified and processed successfully"})