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
    "video_one_time": {"amount": 3900, "plan_id": 0},      # ₹39 early-user offer
    "starter_pack": {"amount": 1300, "plan_id": 0},        # ₹13
    "resume_boost_pack": {"amount": 2500, "plan_id": 0},   # ₹25
    "interview_pack": {"amount": 4900, "plan_id": 0},      # ₹49
    "combo_pack": {"amount": 9900, "plan_id": 0},          # ₹99
}

SUBSCRIPTION_PLAN_NAMES = {"pro", "pro_plus"}
TEST_MODE_CREDIT_BLOCK_DETAIL = "Credits cannot be added because Razorpay is currently in test mode."


def _ensure_crediting_allowed() -> None:
    if not settings.RAZORPAY_CREDITING_ENABLED or RAZORPAY_KEY_ID.startswith("rzp_test_"):
        logger.warning(
            "Blocking Razorpay credit allocation: crediting_enabled=%s, key_mode=%s",
            settings.RAZORPAY_CREDITING_ENABLED,
            "test" if RAZORPAY_KEY_ID.startswith("rzp_test_") else "live",
        )
        raise HTTPException(status_code=403, detail=TEST_MODE_CREDIT_BLOCK_DETAIL)


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


async def process_verified_payment(
    db: AsyncSession,
    user_id: int,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    plan_name: str,
    amount_paise: int
) -> bool:
    """
    Credits user, logs event, and saves ProcessedPayment in a thread-safe transaction.
    Returns True if successfully processed, or False if already processed.
    """
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if not user:
        logger.error("User %s not found during payment processing", user_id)
        raise ValueError("User not found")

    # Re-verify replay protection under row lock to prevent race conditions
    existing_payment_stmt = select(ProcessedPayment).where(
        (ProcessedPayment.razorpay_payment_id == razorpay_payment_id) |
        (ProcessedPayment.razorpay_order_id == razorpay_order_id)
    )
    duplicate_res = await db.execute(existing_payment_stmt)
    if duplicate_res.scalar_one_or_none():
        logger.warning(
            "Duplicate payment attempt after user lock for payment_id %s or order_id %s",
            razorpay_payment_id,
            razorpay_order_id,
        )
        return False

    # Individual Packs
    if plan_name == "ats_one_time":
        user.purchased_ats_credits = (user.purchased_ats_credits or 0) + 3
    elif plan_name == "tailor_one_time":
        user.purchased_tailor_credits = (user.purchased_tailor_credits or 0) + 3
    elif plan_name == "audio_one_time":
        user.audio_interviews_left = (user.audio_interviews_left or 0) + 3
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
        logger.error("Invalid plan name during credit allocation: %s", plan_name)
        raise ValueError("Invalid plan")

    # Save the processed payment to database to prevent future replay attacks
    processed_payment = ProcessedPayment(
        razorpay_payment_id=razorpay_payment_id,
        razorpay_order_id=razorpay_order_id,
        user_id=user.id,
        amount=amount_paise
    )
    db.add(processed_payment)
    await db.flush() # Ensure DB constraint is verified

    # Structured Payment Logging
    amount_paid = amount_paise / 100.0 # Convert from paise to INR
    log_payment_event("PAYMENT_SUCCESS", str(user.id), amount_paid, f"PLAN: {plan_name}")
    
    logger.info("Payment processed successfully for user %s: plan=%s", user.id, plan_name)
    return True


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

    try:
        _ensure_crediting_allowed()
        success = await process_verified_payment(
            db=db,
            user_id=current_user.id,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            plan_name=plan_name,
            amount_paise=expected_amount
        )
        if not success:
            raise HTTPException(status_code=400, detail="This payment has already been processed")
        await db.commit()
    except ValueError as val_err:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(val_err))
    except IntegrityError as ie:
        await db.rollback()
        logger.warning("Duplicate payment integrity constraint triggered: %s", ie)
        raise HTTPException(status_code=400, detail="This payment has already been processed")
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Error processing payment in verify_payment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not process payment")

    return JSONResponse(content={"message": "Payment verified and processed successfully"})


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")
    
    signature = request.headers.get("X-Razorpay-Signature")
    if not signature:
        logger.warning("Webhook missing X-Razorpay-Signature header")
        raise HTTPException(status_code=400, detail="Missing signature header")

    try:
        client.utility.verify_webhook_signature(
            body_str,
            signature,
            settings.RAZORPAY_WEBHOOK_SECRET
        )
    except Exception as e:
        logger.warning("Webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        event_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = event_data.get("event")
    if event != "payment.captured":
        logger.info("Webhook received non-payment.captured event: %s. Ignoring.", event)
        return {"status": "ignored", "event": event}

    payload = event_data.get("payload", {})
    payment_entity = payload.get("payment", {}).get("entity", {})
    
    payment_id = payment_entity.get("id")
    order_id = payment_entity.get("order_id")
    amount = payment_entity.get("amount")
    currency = payment_entity.get("currency")
    status = payment_entity.get("status")
    
    notes = payment_entity.get("notes", {})
    user_id_str = notes.get("user_id")
    plan_name = notes.get("plan_name")

    if not all([payment_id, order_id, amount, user_id_str, plan_name]):
        logger.warning("Webhook payment entity is missing required fields")
        return {"status": "ignored", "detail": "Missing required fields in payment entity"}

    if currency != "INR":
        logger.warning("Webhook received payment in invalid currency: %s", currency)
        return {"status": "ignored", "detail": "Invalid currency"}

    if status != "captured":
        logger.warning("Webhook payment status is not captured: %s", status)
        return {"status": "ignored", "detail": "Payment not captured"}

    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.warning("Webhook user_id is not a valid integer: %s", user_id_str)
        return {"status": "ignored", "detail": "Invalid user_id"}

    expected_amount = PLAN_PRICES.get(plan_name, {}).get("amount")
    if expected_amount is None:
        logger.warning("Webhook received unknown plan: %s", plan_name)
        return {"status": "ignored", "detail": "Invalid plan name"}

    if int(amount) != expected_amount:
        logger.warning("Webhook amount mismatch: expected %d, got %s", expected_amount, amount)
        return {"status": "ignored", "detail": "Amount mismatch"}

    try:
        _ensure_crediting_allowed()
        success = await process_verified_payment(
            db=db,
            user_id=user_id,
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            plan_name=plan_name,
            amount_paise=int(amount)
        )
        if success:
            await db.commit()
            logger.info("Webhook successfully processed payment %s", payment_id)
            return {"status": "processed", "payment_id": payment_id}
        else:
            logger.info("Webhook duplicate payment %s already processed. Ignoring.", payment_id)
            return {"status": "already_processed", "payment_id": payment_id}
    except HTTPException as e:
        await db.rollback()
        logger.warning("Webhook payment not credited: %s", e.detail)
        return {"status": "credit_blocked", "detail": e.detail}
    except Exception as e:
        await db.rollback()
        logger.error("Error processing payment in webhook: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error during payment processing")
