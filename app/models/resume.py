from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

class ResumeHistory(Base):
    __tablename__ = "resume_history"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    history_type = Column(String, nullable=False)  # 'ats' or 'tailor'
    result_data = Column(JSON, nullable=False)     # ATS scores or Tailored resume metadata
    file_path = Column(String, nullable=True)     # Path to PDF for tailored resumes
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")
