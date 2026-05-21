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

GEMINI_MODELS = [
    'gemini-2.5-flash',
    'gemini-3.0-flash',
    'gemini-3.1-flash-lite-preview'
]


def _is_retryable_ai_error(err: Exception) -> bool:
    err_str = str(err).lower()
    retryable_markers = (
        "500",
        "502",
        "503",
        "504",
        "internal",
        "deadline",
        "timeout",
        "temporarily",
        "overloaded",
        "unavailable",
        "high demand",
    )
    return any(marker in err_str for marker in retryable_markers)


def _raise_ai_http_exception(err: Exception, context: str) -> None:
    from fastapi import HTTPException

    err_str = str(err)
    err_lower = err_str.lower()
    if "429" in err_str or "quota" in err_lower:
        raise HTTPException(
            status_code=429,
            detail="AI service is temporarily rate-limited. Please wait a moment and try again."
        )
    if _is_retryable_ai_error(err):
        raise HTTPException(
            status_code=503,
            detail="AI service is temporarily unavailable. Please try again shortly."
        )
    logger.error("%s failed after all fallbacks: %s", context, err_str)
    raise HTTPException(status_code=500, detail="AI service failed. Please try again.")


class InterviewService:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model_id = 'gemini-2.5-flash'

    async def _generate_content_with_fallback(self, prompt: str, system_instruction: str = None, response_mime_type: str = None, temperature: float = 0.8) -> str:
        last_err = None
        for model in GEMINI_MODELS:
            attempts = 2
            for attempt in range(1, attempts + 1):
                try:
                    logger.info(">>> LLM CALL START | Model: %s | Prompt chars: %d | Attempt: %d", model, len(prompt), attempt)
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
                    logger.info("<<< LLM CALL SUCCESS | Model: %s | Attempt: %d", model, attempt)
                    return response.text
                except Exception as e:
                    last_err = e
                    if attempt < attempts and _is_retryable_ai_error(e):
                        logger.warning("Retryable LLM failure for model %s on attempt %d: %s. Retrying...", model, attempt, e)
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    logger.warning("LLM call failed for model %s on attempt %d: %s. Trying fallback...", model, attempt, e)
                    break

        _raise_ai_http_exception(last_err, "AI service")

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
            question_number=new_session.question_number,
            total_questions=8,
            warnings_count=new_session.warnings_count or 0,
            is_active=new_session.is_active
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
        served_at = session.question_started_at or session.updated_at
        if served_at.tzinfo is None:
            served_at = served_at.replace(tzinfo=timezone.utc)
        
        elapsed_seconds = (now - served_at).total_seconds()
        if elapsed_seconds > max_allowed:
            if retakes.get(retake_key):
                max_allowed = 300.0
            elif elapsed_seconds <= 360.0:
                max_allowed = 360.0
                retakes[retake_key] = True
                session.retakes = retakes

        if elapsed_seconds > max_allowed:
            current_q = session.current_question
            new_history = list(session.history or [])
            new_history.append({
                "question": current_q,
                "analysis": (
                    "Candidate failed to answer this question within the time limit. "
                    f"Elapsed time: {elapsed_seconds:.0f} seconds."
                ),
                "status": "timed_out",
            })
            session.history = new_history

            try:
                next_q = await self._generate_next_question(db, session_id)
            except Exception:
                session.processing_status = "failed"
                await db.commit()
                raise

            ended = next_q == "INTERVIEW_END"
            if ended:
                session.is_active = False
                session.processing_status = "completed"
            else:
                session.processing_status = "idle"

            logger.info(
                "Video response timed out for session %s, question %s after %.0fs; advanced to question %s",
                session_id,
                retake_key,
                elapsed_seconds,
                session.question_number,
            )

            return InterviewResponse(
                session_id=session_id,
                question=next_q if not ended else "",
                question_number=session.question_number,
                total_questions=8,
                interview_ended=ended,
                warnings_count=session.warnings_count or 0,
                is_active=session.is_active,
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
            interview_ended=ended,
            warnings_count=session.warnings_count or 0,
            is_active=session.is_active
        )

    async def skip_response(self, db: AsyncSession, session_id: str, reason: str = "Candidate failed to answer this question within the time limit.") -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Invalid or inactive session ID")

        current_q = session.current_question
        logger.info("Skipping video response for session %s, question %s: %s", session_id, session.question_number, reason)

        new_history = list(session.history or [])
        new_history.append({
            "question": current_q,
            "analysis": reason
        })
        session.history = new_history

        next_q = await self._generate_next_question(db, session_id)
        ended = next_q == "INTERVIEW_END"

        if ended:
            session.is_active = False
            session.processing_status = "completed"
        else:
            session.processing_status = "idle"

        return InterviewResponse(
            session_id=session_id,
            question=next_q if not ended else "",
            question_number=session.question_number,
            total_questions=8,
            interview_ended=ended,
            warnings_count=session.warnings_count or 0,
            is_active=session.is_active
        )

    async def grant_resume_grace(self, db: AsyncSession, session_id: str) -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Invalid or inactive session ID")

        retakes = dict(session.retakes or {})
        grace_key = "video_resume_grace_used"
        if not retakes.get(grace_key):
            retakes[grace_key] = {
                "question_number": session.question_number,
                "granted_at": datetime.now(timezone.utc).isoformat(),
            }
            session.retakes = retakes
            session.question_started_at = datetime.now(timezone.utc)
            logger.info("Granted one-time video resume grace for session %s, question %s", session_id, session.question_number)

        return InterviewResponse(
            session_id=session_id,
            question=session.current_question or "Starting interview...",
            question_number=session.question_number,
            total_questions=8,
            interview_ended=not session.is_active,
            warnings_count=session.warnings_count or 0,
            is_active=session.is_active
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
        session.question_started_at = datetime.now(timezone.utc)
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
        
        last_err = None
        for model in GEMINI_MODELS:
            attempts = 2
            for attempt in range(1, attempts + 1):
                try:
                    logger.info(">>> LLM CALL START [_analyze_video] | Model: %s | Attempt: %d", model, attempt)
                    response = await self.client.aio.models.generate_content(
                        model=model,
                        contents=[prompt, video_file]
                    )
                    logger.info("<<< LLM CALL SUCCESS [_analyze_video] | Model: %s | Attempt: %d", model, attempt)
                    return response.text
                except Exception as e:
                    last_err = e
                    if attempt < attempts and _is_retryable_ai_error(e):
                        logger.warning(
                            "Retryable video analysis failure for model %s on attempt %d: %s. Retrying...",
                            model,
                            attempt,
                            e
                        )
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    logger.warning("LLM call failed for model %s during video analysis on attempt %d: %s. Trying fallback...", model, attempt, e)
                    break

        _raise_ai_http_exception(last_err, "AI video analysis")

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
