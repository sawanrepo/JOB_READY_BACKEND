from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from app.models.subscription import SubscriptionPlan
from app.config import settings

# 🔄 Convert asyncpg to psycopg2
DATABASE_URL = settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def seed_subscription_plans():
    db = SessionLocal()

    if not db.query(SubscriptionPlan).first():
        plans = [
            SubscriptionPlan(name="Free", max_ats_checks=1, max_resume_tailoring=2, max_mock_interviews=1),
            SubscriptionPlan(name="Pro", max_ats_checks=5, max_resume_tailoring=7, max_mock_interviews=5),
            SubscriptionPlan(name="Pro Plus", max_ats_checks=10, max_resume_tailoring=14, max_mock_interviews=10),
        ]
        db.add_all(plans)
        db.commit()

    db.close()

if __name__ == "__main__":
    seed_subscription_plans()