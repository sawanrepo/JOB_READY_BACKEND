from pydantic import BaseModel
from typing import List, Optional

class ResumeAnalysisRequest(BaseModel):
    job_description: str

class ResumeAnalysisResponse(BaseModel):
    ats_score: float
    keyword_match_percentage: float
    matched_skills: List[str] = []
    missing_skills: List[str] = []
    job_title_match: Optional[str] = None
    experience_alignment: Optional[str] = None
    education_alignment: Optional[str] = None
    resume_format_compliance: List[str] = []
    action_verbs_count: int = 0
    red_flags: List[str] = []
    improvement_suggestions: List[str] = []
    is_resume: bool = True
    error_message: Optional[str] = None


class PersonalInfo(BaseModel):
    location: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    github: Optional[str] = None
    github_url: Optional[str] = None
    linkedin: Optional[str] = None
    linkedin_url: Optional[str] = None
    portfolio: Optional[str] = None
    portfolio_url: Optional[str] = None

class ExperienceItem(BaseModel):
    role: Optional[str] = None
    company: Optional[str] = None
    duration: Optional[str] = None
    details: Optional[List[str]] = None

class ProjectItem(BaseModel):
    title: Optional[str] = None
    tech_stack: Optional[str] = None
    details: Optional[List[str]] = None

class EducationItem(BaseModel):
    degree: Optional[str] = None
    institution: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

class CertificationItem(BaseModel):
    title: Optional[str] = None
    link: Optional[str] = None
    date: Optional[str] = None


class TailoredContent(BaseModel):
    name: Optional[str] = None
    tagline: Optional[str] = None
    personal_info: Optional[PersonalInfo] = None
    summary: Optional[str] = None
    skills: Optional[dict] = None
    experience: Optional[List[ExperienceItem]] = None
    projects: Optional[List[ProjectItem]] = None
    education: Optional[List[EducationItem]] = None
    certifications: Optional[List[CertificationItem]] = None

class TailoredResumeResponse(BaseModel):
    tailored_content: Optional[TailoredContent] = None
    filename: Optional[str] = None
    pdf_url: Optional[str] = None
    is_resume: bool = True
    error_message: Optional[str] = None