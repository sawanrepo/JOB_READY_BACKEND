from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.models.user import User
from app.models.subscription import SubscriptionPlan
from app.utils.auth import get_current_user

router = APIRouter()

@router.post("/subscribe/{plan_name}")
async def subscribe(plan_name: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    raise HTTPException(status_code=501, detail="Subscriptions are not available yet.")
