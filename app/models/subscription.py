from sqlalchemy import Column, Integer, String
from app.database import Base

class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)  
    max_ats_checks = Column(Integer, nullable=False)
    max_resume_tailoring = Column(Integer, nullable=False)
    max_mock_interviews = Column(Integer, nullable=False)