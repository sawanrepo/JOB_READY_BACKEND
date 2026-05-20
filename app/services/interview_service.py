import os
from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.prompts import INTERVIEW_SYSTEM_PROMPT, INTERVIEW_ANALYSIS_PROMPT, INTERVIEW_REPORT_PROMPT
from app.models.interview import InterviewSession
import json
import logging
import asyncio
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class InterviewService:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model_id = 'gemini-2.5-flash'

    async def _generate_content_with_fallback(self, prompt: str, system_instruction: str = None, response_mime_type: str = None, temperature: float = 0.8) -> str:
        # Try gemini-2.5-flash first, then try highly stable backups
        models = [
            'gemini-2.5-flash',
            'gemini-3.0-flash',
            'gemini-3.1-flash-lite-preview'
        ]
        
        last_err = None
        for model in models:
            try:
                logger.info(">>> LLM CALL START | Model: %s | Prompt chars: %d", model, len(prompt))
                config_args = {"temperature": temperature}
                if system_instruction:
                    config_args["system_instruction"] = system_instruction
                if response_mime_type:
                    config_args["response_mime_type"] = response_mime_type
                    
                response = await self.client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_args)
                )
                logger.info("<<< LLM CALL SUCCESS | Model: %s", model)
                return response.text
            except Exception as e:
                logger.warning("LLM call failed for model %s: %s. Trying fallback...", model, e)
                last_err = e
        
        # If all models failed, raise clean exception
        err_str = str(last_err)
        from fastapi import HTTPException
        if "429" in err_str or "quota" in err_str.lower():
            raise HTTPException(
                status_code=429,
                detail="AI service is temporarily rate-limited. Please wait a moment and try again."
            )
        if "503" in err_str or "overloaded" in err_str.lower() or "experiencing high demand" in err_str.lower():
            raise HTTPException(
                status_code=503,
                detail="AI service is temporarily experiencing high demand. Please try again shortly."
            )
        logger.error("AI service error after all video fallbacks: %s", err_str)
        raise HTTPException(status_code=500, detail="AI service failed. Please try again.")

    async def start_interview(self, db: AsyncSession, user_id: int, request: InterviewStartRequest) -> InterviewResponse:
        # Check for any existing active session for this user
        existing_stmt = select(InterviewSession).where(
            InterviewSession.user_id == user_id,
            InterviewSession.interview_type == "video",
            InterviewSession.is_active == True
        )
        existing_result = await db.execute(existing_stmt)
        if existing_result.scalar_one_or_none():
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400, 
                detail="You already have an active video interview session. Please complete or resume it before starting a new one."
            )

        await db.execute(
            delete(InterviewSession).where(
                InterviewSession.user_id == user_id,
                InterviewSession.interview_type == "video",
                InterviewSession.is_active == False,
                InterviewSession.warnings_count >= 5,
            )
        )

        session_id = uuid.uuid4().hex

        
        # Initialize session in DB
        new_session = InterviewSession(
            id=session_id,
            user_id=user_id,
            interview_type="video",
            resume_text=request.resume_text,
            job_description=request.job_description,
            history=[],
            question_number=0,
            is_active=True,
            retakes={},
            processing_status="idle",
            result_status="pending",
        )
        db.add(new_session)
        await db.flush() # Get session_id into DB context
        
        # Generate first question
        logger.info(f"Starting persistent interview session {session_id} for user {user_id}")
        question = await self._generate_next_question(db, session_id)
        
        return InterviewResponse(
            session_id=session_id,
            question=question,
            question_number=1,
            total_questions=8
        )

    async def process_response(self, db: AsyncSession, session_id: str, video_path: str, retaken: bool = False) -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Invalid or inactive session ID")

        # Retake grace is decided from server-side session state, never from the client flag.
        retakes = dict(session.retakes or {})
        retake_key = str(session.question_number)
        max_allowed = 300.0
        now = datetime.now(timezone.utc)
        served_at = session.updated_at
        if served_at.tzinfo is None:
            served_at = served_at.replace(tzinfo=timezone.utc)
        
        elapsed_seconds = (now - served_at).total_seconds()
        if elapsed_seconds > max_allowed:
            if retakes.get(retake_key):
                raise ValueError("Answer submission timed out. The retake allowance for this question has already been used.")
            max_allowed = 360.0
            if elapsed_seconds <= max_allowed:
                retakes[retake_key] = True
                session.retakes = retakes

        if elapsed_seconds > max_allowed:
            raise ValueError(
                f"Security alert: Answer submission timed out. You took {elapsed_seconds:.0f} seconds, which exceeds the maximum allowed time of {max_allowed:.0f} seconds."
            )
        
        current_q = session.current_question

        logger.info(f"Processing response for session {session_id}, question {session.question_number}")

        # 1. Analyze video response
        session.processing_status = "processing"
        try:
            analysis = await self._analyze_video(video_path, current_q)
        except Exception:
            session.processing_status = "failed"
            await db.commit()
            raise
        
        # 2. Update history in DB
        new_history = list(session.history)
        new_history.append({
            "question": current_q,
            "analysis": analysis
        })
        session.history = new_history
        
        # 3. Generate next question
        next_q = await self._generate_next_question(db, session_id)
        
        ended = next_q == "INTERVIEW_END"
        
        if ended:
             session.is_active = False
             session.processing_status = "completed"
             logger.info(f"Interview ended for session {session_id}")
        else:
             session.processing_status = "idle"

        return InterviewResponse(
            session_id=session_id,
            question=next_q if not ended else "",
            question_number=session.question_number,
            total_questions=8,
            interview_ended=ended
        )

    async def _generate_next_question(self, db: AsyncSession, session_id: str) -> str:
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        session.question_number += 1
        
        if session.question_number > 8:
            return "INTERVIEW_END"

        # Construct prompt
        history_text = "\n".join([f"Q: {h['question']}\nAnalysis: {h['analysis']}" for h in session.history])
        prompt = INTERVIEW_SYSTEM_PROMPT.format(
            question_number=session.question_number,
            total_questions=8,
            last_question=session.current_question if session.current_question else "None (Start of Interview)",
            resume_summary=session.resume_text[:800] + "..." if len(session.resume_text) > 800 else session.resume_text,
            jd_summary=session.job_description[:800] + "..." if len(session.job_description) > 800 else session.job_description,
            history=history_text if history_text else "None"
        )
        
        try:
            next_q = await self._generate_content_with_fallback(
                prompt=prompt,
                temperature=0.8
            )
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [_generate_next_question]: %s", e, exc_info=True)
            raise
            
        session.current_question = next_q.strip()
        return session.current_question

    async def _analyze_video(self, video_path: str, question: str) -> str:
        logger.info(f"Uploading video {video_path} to Gemini...")
        video_file = await self.client.aio.files.upload(file=video_path)
        
        deadline = asyncio.get_running_loop().time() + settings.GEMINI_FILE_PROCESSING_TIMEOUT_SECONDS
        while True:
             file_info = await self.client.aio.files.get(name=video_file.name)
             if file_info.state.name != 'PROCESSING':
                  break
             if asyncio.get_running_loop().time() >= deadline:
                  raise TimeoutError("Video processing timed out")
             await asyncio.sleep(2)

        if file_info.state.name == 'FAILED':
             raise ValueError("Video processing failed by Gemini")

        prompt = INTERVIEW_ANALYSIS_PROMPT.format(question=question)
        
        # Fallback for video analysis call
        models = [
            'gemini-2.5-flash',
            'gemini-3.0-flash',
            'gemini-3.1-flash-lite-preview'
        ]
        
        last_err = None
        for model in models:
            try:
                logger.info(">>> LLM CALL START [_analyze_video] | Model: %s", model)
                response = await self.client.aio.models.generate_content(
                    model=model,
                    contents=[prompt, video_file]
                )
                logger.info("<<< LLM CALL SUCCESS [_analyze_video] | Model: %s", model)
                return response.text
            except Exception as e:
                logger.warning("LLM call failed for model %s during video analysis: %s. Trying fallback...", model, e)
                last_err = e
                
        err_str = str(last_err)
        from fastapi import HTTPException
        if "429" in err_str or "quota" in err_str.lower():
            raise HTTPException(
                status_code=429,
                detail="AI service is temporarily rate-limited. Please wait a moment and try again."
            )
        if "503" in err_str or "overloaded" in err_str.lower() or "experiencing high demand" in err_str.lower():
            raise HTTPException(
                status_code=503,
                detail="AI service is temporarily experiencing high demand. Please try again shortly."
            )
        logger.error("AI video analysis failed after all fallbacks: %s", err_str)
        raise HTTPException(status_code=500, detail="AI video analysis failed. Please try again.")

    async def generate_result(self, db: AsyncSession, session_id: str) -> InterviewResult:
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Invalid session ID")

        # DISCARD logic: No result if warnings reach 5
        if (session.warnings_count or 0) >= 5:
            session.is_active = False
            await db.commit()
            raise ValueError("Interview discarded due to malpractice (too many warnings). No result generated.")

        history_text = "\n".join([f"Q: {h['question']}\nAnalysis: {h['analysis']}" for h in session.history])
        
        prompt = INTERVIEW_REPORT_PROMPT.format(
            resume_text=session.resume_text,
            job_description=session.job_description,
            history=history_text
        )
        
        try:
            content_text = await self._generate_content_with_fallback(
                prompt=prompt,
                response_mime_type="application/json"
            )
            session.is_active = False # Deactivate after completion
            raw_result = json.loads(content_text)
            
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
            logger.error("!!! LLM CALL FAILED [generate_result]: %s", e, exc_info=True)
            from fastapi import HTTPException
            if isinstance(e, HTTPException):
                raise e
            raise ValueError("Failed to generate valid report JSON")
