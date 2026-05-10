from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

class InterviewResult(Base):
    __tablename__ = "interview_results"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    interview_type = Column(String, nullable=False) # "audio" or "video"
    result_data = Column(JSON, nullable=False) # The JSON result
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    user = relationship("User", backref="interview_results")
