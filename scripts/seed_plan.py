import os
import sys

from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.subscription import SubscriptionPlan
from app.config import settings

def sync_database_url() -> str:
    database_url = settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")
    if "ssl=require" in database_url and "sslmode=" not in database_url:
        database_url = database_url.replace("ssl=require", "sslmode=require")
    return database_url

engine = create_engine(sync_database_url())
SessionLocal = sessionmaker(bind=engine)

def seed_subscription_plans():
    db = SessionLocal()

    if not db.query(SubscriptionPlan).first():
        plans = [
            SubscriptionPlan(name="Free", max_ats_checks=1, max_resume_tailoring=1, max_mock_interviews=1),
            SubscriptionPlan(name="Pro", max_ats_checks=5, max_resume_tailoring=7, max_mock_interviews=5),
            SubscriptionPlan(name="Pro Plus", max_ats_checks=10, max_resume_tailoring=14, max_mock_interviews=10),
        ]
        db.add_all(plans)
        db.commit()

    db.close()

if __name__ == "__main__":
    seed_subscription_plans()
