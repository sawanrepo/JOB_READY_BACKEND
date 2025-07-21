from pydantic import BaseModel
from typing import List

class ResumeAnalysisRequest(BaseModel):
    job_description: str

class ResumeAnalysisResponse(BaseModel):
    ats_score: float
    keyword_match_percentage: float
    matched_skills: List[str]
    missing_skills: List[str]
    job_title_match: str
    experience_alignment: str
    education_alignment: str
    resume_format_compliance: List[str]
    action_verbs_count: int
    red_flags: List[str]
    improvement_suggestions: List[str]

class TailoredResumeResponse(BaseModel):
    tailored_content: str
    pdf_url: str