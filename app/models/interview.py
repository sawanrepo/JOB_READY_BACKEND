from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Boolean, Index, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

class InterviewResult(Base):
    __tablename__ = "interview_results"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    session_id = Column(String, unique=True, index=True, nullable=True) # ID of the session this result belongs to
    interview_type = Column(String, nullable=False) # "audio" or "video"
    result_data = Column(JSON, nullable=False) # The JSON result
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    user = relationship("User", backref="interview_results")

class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (
        Index(
            "uq_interview_sessions_user_type_active",
            "user_id",
            "interview_type",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )
    
    id = Column(String, primary_key=True, index=True) # session_id (hex/uuid)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    interview_type = Column(String, nullable=False) # "audio" or "video"
    resume_text = Column(String, nullable=False)
    job_description = Column(String, nullable=False)
    history = Column(JSON, server_default='[]') # List of {question, response_analysis}
    questions = Column(JSON, nullable=True) # For audio interview (list of 10 questions)
    question_number = Column(Integer, server_default='0')
    current_question = Column(String, nullable=True)
    warnings_count = Column(Integer, server_default='0')
    is_active = Column(Boolean, server_default='1')
    retakes = Column(JSON, server_default='{}')
    processing_status = Column(String, server_default='idle')
    result_status = Column(String, server_default='pending')
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    user = relationship("User", back_populates="interview_sessions")

# Update User model back_populates if needed, but backref/back_populates works.
