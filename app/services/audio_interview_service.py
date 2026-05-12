import os
import json
import logging
import asyncio
import google.generativeai as genai
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.prompts import AUDIO_INTERVIEW_QUESTIONS_PROMPT, AUDIO_INTERVIEW_REPORT_PROMPT
from app.models.interview import InterviewSession

logger = logging.getLogger(__name__)

class AudioInterviewService:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-2.5-flash')

    async def start_audio_interview(self, db: AsyncSession, user_id: int, request: InterviewStartRequest) -> dict:
        # Check for any existing active session for this user
        existing_stmt = select(InterviewSession).where(
            InterviewSession.user_id == user_id,
            InterviewSession.is_active == True
        )
        existing_result = await db.execute(existing_stmt)
        if existing_result.scalar_one_or_none():
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400, 
                detail="You already have an active interview session. Please complete or resume it before starting a new one."
            )

        session_id = os.urandom(4).hex()

        
        # 1. Generate 10 questions at once
        prompt = AUDIO_INTERVIEW_QUESTIONS_PROMPT.format(
            resume_text=request.resume_text[:1000] + "..." if len(request.resume_text) > 1000 else request.resume_text,
            job_description=request.job_description[:1000] + "..." if len(request.job_description) > 1000 else request.job_description
        )
        
        try:
            logger.info(">>> LLM CALL START [start_audio_interview] | Model: gemini-2.5-flash")
            response = await self.model.generate_content_async(
                prompt,
                generation_config={"temperature": 0.8}
            )
            logger.info("<<< LLM CALL SUCCESS [start_audio_interview]")
            content = response.text.strip()
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()
            questions = json.loads(content)
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [start_audio_interview]: %s", e, exc_info=True)
            raise ValueError("Failed to generate questions. Please try again.")

        if not isinstance(questions, list) or len(questions) == 0:
            raise ValueError("Invalid questions format returned by AI.")

        # 2. Save session in DB
        new_session = InterviewSession(
            id=session_id,
            user_id=user_id,
            interview_type="audio",
            resume_text=request.resume_text,
            job_description=request.job_description,
            questions=questions,
            is_active=True
        )
        db.add(new_session)
        await db.flush()
        
        return {
            "session_id": session_id,
            "questions": questions,
            "total_questions": len(questions)
        }

    async def generate_result(self, db: AsyncSession, session_id: str, audio_path: str) -> dict:
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Invalid session ID")
        
        logger.info(f"Uploading merged audio {audio_path} to Gemini...")
        audio_file = await asyncio.to_thread(
            genai.upload_file, 
            path=audio_path,
            mime_type='audio/webm'
        )
        
        while True:
             file_info = await asyncio.to_thread(genai.get_file, audio_file.name)
             if file_info.state.name != 'PROCESSING':
                  break
             await asyncio.sleep(2)

        if file_info.state.name == 'FAILED':
             raise ValueError("Audio processing failed by Gemini")

        prompt = AUDIO_INTERVIEW_REPORT_PROMPT.format(
            resume_text=session.resume_text,
            job_description=session.job_description,
            questions_json=json.dumps(session.questions, indent=2)
        )
        
        try:
            generation_config = {"response_mime_type": "application/json"}
            response = await self.model.generate_content_async(
                contents=[prompt, audio_file],
                generation_config=generation_config
            )
            session.is_active = False # Deactivate after completion
            raw_result = json.loads(response.text)
            
            # Manual Sanitization to prevent ResponseValidationError
            sanitized = {
                "communication_score": raw_result.get("communication_score") or raw_result.get("communication", 0),
                "technical_knowledge_score": raw_result.get("technical_knowledge_score") or raw_result.get("technical_knowledge", 0),
                "problem_solving_score": raw_result.get("problem_solving_score") or raw_result.get("problem_solving", 0),
                "confidence_score": raw_result.get("confidence_score") or raw_result.get("confidence", 0),
                "strengths": raw_result.get("strengths") or [],
                "weaknesses": raw_result.get("weaknesses") or [],
                "final_verdict": raw_result.get("final_verdict") or "Needs Improvement",
                "feedback_summary": raw_result.get("feedback_summary") or ""
            }
            
            # Ensure improvement_suggestions is a list
            suggestions = raw_result.get("improvement_suggestions") or []
            if isinstance(suggestions, str):
                sanitized["improvement_suggestions"] = [suggestions]
            else:
                sanitized["improvement_suggestions"] = suggestions
                
            return sanitized

        except Exception as e:
            logger.error("!!! LLM CALL FAILED [generate_audio_result]: %s", e, exc_info=True)
            raise ValueError("Failed to generate valid report JSON")
