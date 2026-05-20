import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.user import User


FREE_SUBSCRIPTION_ID = 1
FREE_ATS_CHECKS_PER_DAY = 1
FREE_RESUME_TAILORING_PER_WEEK = 1
WEEKLY_RESET_WEEKDAY = 6  # Sunday, UTC


def _database_url() -> str:
    database_url = os.environ["DATABASE_URL"]
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if "ssl=require" in database_url and "sslmode=" not in database_url:
        database_url = database_url.replace("ssl=require", "sslmode=require")
    return database_url


engine = create_engine(_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def reset_all_usage(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    weekly_reset = now.weekday() == WEEKLY_RESET_WEEKDAY

    db = SessionLocal()
    try:
        update_values = {
            User.subscription_id: FREE_SUBSCRIPTION_ID,
            User.subscription_expires_at: None,
            User.ats_checks_left_today: FREE_ATS_CHECKS_PER_DAY,
            User.last_ats_check_at: now,
        }

        if weekly_reset:
            update_values[User.resume_tailoring_left_this_week] = FREE_RESUME_TAILORING_PER_WEEK
            update_values[User.last_resume_tailoring_at] = now

        users_updated = db.query(User).update(update_values, synchronize_session=False)
        db.commit()

        result = {
            "users_updated": users_updated,
            "ats_checks_left_today": FREE_ATS_CHECKS_PER_DAY,
            "weekly_reset": weekly_reset,
            "resume_tailoring_left_this_week": FREE_RESUME_TAILORING_PER_WEEK if weekly_reset else "unchanged",
            "reset_at": now.isoformat(),
        }
        print(f"Usage reset complete: {result}")
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def lambda_handler(event, context):
    try:
        result = reset_all_usage()
        return {"status": "success", **result}
    except Exception as exc:
        print(f"Usage reset failed: {exc}")
        return {"status": "error", "message": str(exc)}


if __name__ == "__main__":
    print(reset_all_usage())
