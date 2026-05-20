# ✅ This is a Lambda-compatible reset script for daily/weekly/monthly usage

from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from app.models.user import User
from datetime import datetime, timezone
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:pass@host:port/dbname")
FREE_SUBSCRIPTION_ID = 1
FREE_ATS_CHECKS_PER_DAY = 1
FREE_RESUME_TAILORING_PER_WEEK = 2

# Create engine and session
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def reset_all_usage():
    db = SessionLocal()
    users = db.query(User).all()
    now = datetime.now(timezone.utc)

    for user in users:
        # Subscriptions are disabled for launch; reset only free-plan base quotas.
        user.subscription_id = FREE_SUBSCRIPTION_ID
        user.subscription_expires_at = None
        user.ats_checks_left_today = FREE_ATS_CHECKS_PER_DAY
        user.last_ats_check_at = now

        # Weekly reset (only run if Sunday)
        if now.weekday() == 6:  # Sunday
            user.resume_tailoring_left_this_week = FREE_RESUME_TAILORING_PER_WEEK
            user.last_resume_tailoring_at = now

        # Monthly reset (only run if 1st)
        if now.day == 1:
            # Mock and Audio interviews are now persistent and not reset monthly
            pass


    db.commit()
    db.close()
    print("✅ User usage limits reset successfully.")

# For AWS Lambda

def lambda_handler(event, context):
    try:
        reset_all_usage()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    reset_all_usage()
