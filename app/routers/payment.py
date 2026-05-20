from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.models.user import User
from app.models.payment import ProcessedPayment
import hmac
import hashlib
import logging
import asyncio
import razorpay
from app.utils.auth import get_current_user
from app.config import settings
from app.utils.logging import log_payment_event
from app.utils.limiter import limiter


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

SUBSCRIPTION_PLAN_NAMES = {"pro", "pro_plus"}


@router.post("/create-order/{plan}")
@limiter.limit("10/minute")
async def create_order(request: Request, plan: str, current_user: User = Depends(get_current_user)):
    if plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if plan in SUBSCRIPTION_PLAN_NAMES:
        raise HTTPException(status_code=403, detail="Subscriptions are not available yet.")

    try:
        order = await asyncio.to_thread(
            client.order.create,
            {
                "amount": PLAN_PRICES[plan]["amount"],
                "currency": "INR",
                "payment_capture": 1,
                "notes": {"user_id": str(current_user.id), "plan_name": plan}
            }
        )
    except Exception as e:
        logger.error("Razorpay order creation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not create payment order. Please try again.")

    return {
        "order_id": order["id"],
        "key": RAZORPAY_KEY_ID,
        "amount": PLAN_PRICES[plan]["amount"],
        "plan": plan,
        "notes": order["notes"]
    }


@router.post("/verify")
@limiter.limit("10/minute")
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

    # Payment replay protection: verify the payment ID or order ID has not been processed already
    existing_payment_stmt = select(ProcessedPayment).where(
        (ProcessedPayment.razorpay_payment_id == razorpay_payment_id) |
        (ProcessedPayment.razorpay_order_id == razorpay_order_id)
    )
    existing_payment_res = await db.execute(existing_payment_stmt)
    if existing_payment_res.scalar_one_or_none():
        logger.warning("Duplicate payment attempt (replay protection triggered) for payment_id %s or order_id %s", razorpay_payment_id, razorpay_order_id)
        raise HTTPException(status_code=400, detail="This payment has already been processed")

    try:
        # Fetch order from Razorpay to verify details and prevent spoofing
        razorpay_order = await asyncio.to_thread(client.order.fetch, razorpay_order_id)
        order_notes = razorpay_order.get("notes", {})
        trusted_plan = order_notes.get("plan_name")
        trusted_user_id = order_notes.get("user_id")

        if trusted_user_id and str(current_user.id) != trusted_user_id:
            logger.warning("User %s trying to verify order created for %s", current_user.id, trusted_user_id)
            raise HTTPException(status_code=403, detail="Order belongs to a different user")

        if trusted_plan:
            plan_name = trusted_plan # Override frontend provided plan name with the trusted one

        if plan_name in SUBSCRIPTION_PLAN_NAMES:
            raise HTTPException(status_code=403, detail="Subscriptions are not available yet.")
            
        # Fetch payment details from Razorpay to verify amount, currency, and capture status
        razorpay_payment = await asyncio.to_thread(client.payment.fetch, razorpay_payment_id)
        if razorpay_payment.get("currency") != "INR":
            logger.warning("Invalid payment currency: %s", razorpay_payment.get("currency"))
            raise HTTPException(status_code=400, detail="Invalid payment currency")

        expected_amount = PLAN_PRICES.get(plan_name, {}).get("amount")
        if expected_amount is None:
            logger.warning("Unknown plan name: %s", plan_name)
            raise HTTPException(status_code=400, detail="Invalid plan")

        if int(razorpay_payment.get("amount")) != expected_amount:
            logger.warning("Payment amount mismatch: expected %d, got %d", expected_amount, razorpay_payment.get("amount"))
            raise HTTPException(status_code=400, detail="Payment amount mismatch")

        if razorpay_payment.get("status") != "captured":
            logger.warning("Payment not captured: status is %s", razorpay_payment.get("status"))
            raise HTTPException(status_code=400, detail="Payment has not been successfully captured")

        if razorpay_payment.get("order_id") != razorpay_order_id:
            logger.warning("Payment order ID mismatch: expected %s, got %s", razorpay_order_id, razorpay_payment.get("order_id"))
            raise HTTPException(status_code=400, detail="Payment order ID mismatch")
            
    except Exception as e:
        logger.error(f"Error fetching/verifying details from Razorpay: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail="Could not verify order/payment details with payment gateway")

    user_stmt = select(User).where(User.id == current_user.id).with_for_update()
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    duplicate_res = await db.execute(existing_payment_stmt)
    if duplicate_res.scalar_one_or_none():
        logger.warning("Duplicate payment attempt after user lock for payment_id %s or order_id %s", razorpay_payment_id, razorpay_order_id)
        raise HTTPException(status_code=400, detail="This payment has already been processed")

    # Individual Packs
    if plan_name == "ats_one_time":
        user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 1
    elif plan_name == "tailor_one_time":
        user.purchased_tailor_credits = (user.purchased_tailor_credits or 0) + 1
    elif plan_name == "audio_one_time":
        user.audio_interviews_left = (user.audio_interviews_left or 0) + 1
    elif plan_name == "video_one_time":
        user.mock_interviews_left = (user.mock_interviews_left or 0) + 1
    
    # Bundles
    elif plan_name == "starter_pack":
        user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 3
        user.purchased_tailor_credits = (user.purchased_tailor_credits or 0) + 1
    elif plan_name == "resume_boost_pack":
        user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 5
        user.purchased_tailor_credits = (user.purchased_tailor_credits or 0) + 3
    elif plan_name == "interview_pack":
        user.audio_interviews_left = (user.audio_interviews_left or 0) + 2
        user.mock_interviews_left = (user.mock_interviews_left or 0) + 1
    elif plan_name == "combo_pack":
        user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 5
        user.purchased_tailor_credits = (user.purchased_tailor_credits or 0) + 5
        user.audio_interviews_left = (user.audio_interviews_left or 0) + 2
        user.mock_interviews_left = (user.mock_interviews_left or 0) + 1

    
    # Legacy plan names (for backward compatibility during transition if needed)
    elif plan_name == "one_time":
        user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 1
    elif plan_name == "mock_interview_one_time":
        user.mock_interviews_left = (user.mock_interviews_left or 0) + 1
    elif plan_name == "audio_interview_one_time":
        user.audio_interviews_left = (user.audio_interviews_left or 0) + 1
    
    else:
        raise HTTPException(status_code=400, detail="Invalid plan")

    # Save the processed payment to database to prevent future replay attacks
    processed_payment = ProcessedPayment(
        razorpay_payment_id=razorpay_payment_id,
        razorpay_order_id=razorpay_order_id,
        user_id=user.id,
        amount=PLAN_PRICES.get(plan_name, {}).get("amount", 0)
    )
    db.add(processed_payment)

    try:
        await db.commit()
    except IntegrityError as ie:
        await db.rollback()
        logger.warning("Duplicate payment integrity constraint triggered: %s", ie)
        raise HTTPException(status_code=400, detail="This payment has already been processed")
    
    # Structured Payment Logging
    amount_paid = PLAN_PRICES.get(plan_name, {}).get("amount", 0) / 100.0 # Convert from paise to INR
    log_payment_event("PAYMENT_SUCCESS", str(user.id), amount_paid, f"PLAN: {plan_name}")
    
    logger.info("Payment processed for user %s: plan=%s", user.id, plan_name)
    return JSONResponse(content={"message": "Payment verified and processed successfully"})
