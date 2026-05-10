from sqlalchemy import Column, Integer, String, Boolean, DateTime, LargeBinary, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
from app.models.subscription import SubscriptionPlan

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(LargeBinary, nullable=True)  # Nullable for OAuth users
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    is_google_oauth = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    google_id = Column(String, unique=True, nullable=True) 
    otp = Column(String, nullable=True)
    otp_created_at = Column(DateTime(timezone=True), nullable=True)

    subscription_id = Column(Integer, ForeignKey("subscription_plans.id"), default=1)
    subscription = relationship("SubscriptionPlan")

    # 🔢 Usage tracking
    ats_checks_left_today = Column(Integer, default=1)
    resume_tailoring_left_this_week = Column(Integer, default=2)
    mock_interviews_left_this_month = Column(Integer, default=1)
    audio_interviews_left_this_month = Column(Integer, default=0)

    # 🕒 Timestamps
    last_ats_check_at = Column(DateTime(timezone=True), nullable=True)
    last_resume_tailoring_at = Column(DateTime(timezone=True), nullable=True)
    last_mock_interview_at = Column(DateTime(timezone=True), nullable=True)
    # expire column
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)