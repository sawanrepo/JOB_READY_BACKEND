# ✅ This is a Lambda-compatible reset script for daily/weekly/monthly usage

from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from app.models.user import User
from datetime import datetime, timezone
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:pass@host:port/dbname")

# Create engine and session
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def reset_all_usage():
    db = SessionLocal()
    users = db.query(User).all()
    now = datetime.now(timezone.utc)

    for user in users:
        # Daily reset for ATS
        user.ats_checks_left_today = user.subscription.max_ats_checks
        user.last_ats_check_at = now

        # Weekly reset (only run if Sunday)
        if now.weekday() == 6:  # Sunday
            user.resume_tailoring_left_this_week = user.subscription.max_resume_tailoring
            user.last_resume_tailoring_at = now

        # Monthly reset (only run if 1st)
        if now.day == 1:
            # Mock interviews are now persistent and not reset monthly
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