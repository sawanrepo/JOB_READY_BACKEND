from pydantic import BaseModel
from typing import List, Dict

class ResumeAnalysisRequest(BaseModel):
    job_description: str

class ResumeAnalysisResponse(BaseModel):
    ats_score: float
    matched_skills: List[str]
    missing_skills: List[str]
    improvement_suggestions: List[str]

class TailoredResumeResponse(BaseModel):
    tailored_content: str
    pdf_url: str