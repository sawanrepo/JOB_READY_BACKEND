from pydantic import BaseModel
from typing import List, Optional

class QuestionAnswerPair(BaseModel):
    question: str
    answer_text: str
    status: Optional[str] = None

class InterviewStartRequest(BaseModel):
    resume_text: str
    job_description: str

class InterviewResponse(BaseModel):
    session_id: str
    question: str
    question_number: int
    total_questions: int = 10
    interview_ended: bool = False
    warnings_count: int = 0
    is_active: bool = True

class InterviewResult(BaseModel):
    communication_score: int
    technical_knowledge_score: int
    problem_solving_score: int
    confidence_score: int
    strengths: List[str]
    weaknesses: List[str]
    improvement_suggestions: List[str]
    final_verdict: str  # Ready / Almost Ready / Needs Improvement
    feedback_summary: str
    warnings_count: Optional[int] = None
    question_answer_pairs: Optional[List[QuestionAnswerPair]] = None
