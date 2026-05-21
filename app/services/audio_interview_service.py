import os
import json
import logging
import asyncio
import uuid
from datetime import datetime, timezone
import boto3
import httpx
from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.prompts import AUDIO_INTERVIEW_QUESTIONS_PROMPT, AUDIO_INTERVIEW_REPORT_PROMPT
from app.models.interview import InterviewSession

logger = logging.getLogger(__name__)

AUDIO_TRANSCRIBE_FORMAT_BY_EXTENSION = {
    ".webm": "webm",
    ".mp3": "mp3",
    ".wav": "wav",
    ".m4a": "m4a",
    ".mp4": "mp4",
    ".ogg": "ogg",
}


def get_transcribe_media_format(audio_path: str) -> str:
    ext = os.path.splitext(audio_path)[1].lower()
    media_format = AUDIO_TRANSCRIBE_FORMAT_BY_EXTENSION.get(ext)
    if not media_format:
        raise ValueError(f"Unsupported audio format for transcription: {ext or 'unknown'}")
    return media_format


class AudioInterviewService:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model_id = 'gemini-2.5-flash'
        
        # AWS Clients
        try:
            self.s3 = boto3.client(
                's3',
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION
            )
            self.transcribe = boto3.client(
                'transcribe',
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION
            )
            if not settings.AWS_S3_BUCKET_NAME:
                logger.warning("AWS_S3_BUCKET_NAME is not set. Audio interviews will fail to transcribe.")
        except Exception as e:
            logger.error(f"Failed to initialize AWS clients: {e}")
            self.s3 = None
            self.transcribe = None

    async def _generate_content_with_fallback(self, prompt: str, system_instruction: str = None, response_mime_type: str = None, temperature: float = 0.8) -> str:
        # Try gemini-2.5-flash first, then try highly stable and lite backups
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
                logger.warning("LLM call failed for model %s: %s. Trying backup model...", model, e)
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
        logger.error("AI service error after all audio fallbacks: %s", err_str)
        raise HTTPException(status_code=500, detail="AI service failed. Please try again.")

    async def start_audio_interview(self, db: AsyncSession, user_id: int, request: InterviewStartRequest) -> dict:
        # Check for any existing active session for this user
        existing_stmt = select(InterviewSession).where(
            InterviewSession.user_id == user_id,
            InterviewSession.interview_type == "audio",
            InterviewSession.is_active == True
        )
        existing_result = await db.execute(existing_stmt)
        if existing_result.scalar_one_or_none():
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400, 
                detail="You already have an active audio interview session. Please complete or resume it before starting a new one."
            )

        await db.execute(
            delete(InterviewSession).where(
                InterviewSession.user_id == user_id,
                InterviewSession.interview_type == "audio",
                InterviewSession.is_active == False,
                InterviewSession.warnings_count >= 5,
            )
        )

        session_id = uuid.uuid4().hex

        # 1. Generate 10 questions at once
        prompt = AUDIO_INTERVIEW_QUESTIONS_PROMPT.format(
            resume_text=request.resume_text[:1000] + "..." if len(request.resume_text) > 1000 else request.resume_text,
            job_description=request.job_description[:1000] + "..." if len(request.job_description) > 1000 else request.job_description
        )
        
        try:
            content_text = await self._generate_content_with_fallback(
                prompt=prompt,
                temperature=0.8
            )
            content = content_text.strip()
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()
            questions = json.loads(content)
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [start_audio_interview]: %s", e, exc_info=True)
            from fastapi import HTTPException
            if isinstance(e, HTTPException):
                raise e
            raise ValueError(f"Failed to generate questions: {str(e)}")

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
            is_active=True,
            history=[], # Initialize empty history
            retakes={},
            processing_status="idle",
            result_status="pending",
            question_started_at=datetime.now(timezone.utc),
        )
        db.add(new_session)
        await db.flush()
        
        return {
            "session_id": session_id,
            "questions": questions,
            "total_questions": len(questions)
        }

    async def save_audio_answer(self, db: AsyncSession, session_id: str, question_index: int, audio_path: str, retaken: bool = False):
        """Saves audio answer metadata and triggers background STT."""
        stmt = select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Session not found")

        # Validate audio question_index to prevent out-of-bounds or manipulation
        if question_index < 0 or question_index >= len(session.questions or []):
            raise ValueError(f"Invalid question index: {question_index}. Total questions: {len(session.questions or [])}")
        current_question_index = session.question_number or 0
        if question_index < current_question_index:
            logger.info(
                "Ignoring duplicate audio answer for session %s, question %s; current question is %s",
                session_id,
                question_index,
                current_question_index,
            )
            return {
                "session_id": session_id,
                "question_text": session.questions[question_index],
                "audio_path": audio_path,
                "question_index": question_index,
                "current_question_index": current_question_index,
                "duplicate": True,
            }
        if question_index != current_question_index:
            raise ValueError(
                f"Invalid question order: expected question index {current_question_index}, got {question_index}"
            )

        # Retake grace is decided from server-side session state, never from the client flag.
        retakes = dict(session.retakes or {})
        retake_key = str(question_index)
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
            questions = session.questions or []
            history = list(session.history or [])
            while len(history) <= question_index:
                history.append(None)

            history[question_index] = {
                "question": questions[question_index],
                "answer_text": "Candidate failed to answer this question within the time limit.",
                "status": "timed_out",
                "elapsed_seconds": round(elapsed_seconds),
                "timestamp": str(uuid.uuid4()),
            }

            session.history = history
            session.question_number = question_index + 1
            session.question_started_at = now
            session.processing_status = "completed" if session.question_number >= len(questions) else "idle"
            await db.commit()

            logger.info(
                "Audio answer timed out for session %s, question %s after %.0fs; advanced to question %s",
                session_id,
                question_index,
                elapsed_seconds,
                session.question_number,
            )

            return {
                "session_id": session_id,
                "question_text": questions[question_index],
                "audio_path": audio_path,
                "question_index": question_index,
                "current_question_index": session.question_number,
                "timed_out": True,
            }

        # Update progress
        session.question_number = question_index + 1
        session.question_started_at = datetime.now(timezone.utc)
        session.processing_status = "processing"
        await db.commit()
        
        # Return necessary data for background task
        return {
            "session_id": session_id,
            "question_text": session.questions[question_index],
            "audio_path": audio_path,
            "question_index": question_index,
            "current_question_index": session.question_number,
        }

    async def skip_audio_answer(self, db: AsyncSession, session_id: str, question_index: int, reason: str = "Candidate failed to answer this question within the time limit."):
        stmt = select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        questions = session.questions or []
        if question_index < 0 or question_index >= len(questions):
            raise ValueError(f"Invalid question index: {question_index}. Total questions: {len(questions)}")

        current_question_index = session.question_number or 0
        if question_index < current_question_index:
            return {
                "session_id": session_id,
                "question_index": question_index,
                "duplicate": True,
            }
        if question_index != current_question_index:
            raise ValueError(
                f"Invalid question order: expected question index {current_question_index}, got {question_index}"
            )

        history = list(session.history or [])
        while len(history) <= question_index:
            history.append(None)

        history[question_index] = {
            "question": questions[question_index],
            "answer_text": reason,
            "status": "skipped",
            "timestamp": str(uuid.uuid4()),
        }
        session.history = history
        session.question_number = question_index + 1
        session.question_started_at = datetime.now(timezone.utc)
        session.processing_status = "completed" if session.question_number >= len(questions) else "idle"
        await db.commit()

        return {
            "session_id": session_id,
            "question_index": question_index,
            "skipped": True,
        }

    async def process_stt_background(self, session_id: str, question_index: int, question_text: str, audio_path: str, db_factory):
        """Background task to handle S3 upload and AWS Transcribe."""
        job_name = f"stt_{session_id}_{question_index}_{uuid.uuid4().hex[:6]}"
        s3_key = None
        job_started = False
        
        try:
            media_format = get_transcribe_media_format(audio_path)
            s3_key = f"audio_interviews/{session_id}/q_{question_index}.{media_format}"

            if not self.s3 or not self.transcribe or not settings.AWS_S3_BUCKET_NAME:
                raise RuntimeError("AWS clients or S3 bucket are not configured for audio transcription")

            # 1. Upload to S3
            logger.info(f"Uploading {audio_path} to S3 bucket {settings.AWS_S3_BUCKET_NAME}...")
            await asyncio.to_thread(
                self.s3.upload_file,
                audio_path,
                settings.AWS_S3_BUCKET_NAME,
                s3_key
            )
            
            # 2. Start Transcribe Job
            media_uri = f"s3://{settings.AWS_S3_BUCKET_NAME}/{s3_key}"
            logger.info(f"Starting Transcribe job {job_name} for {media_uri}")
            await asyncio.to_thread(
                self.transcribe.start_transcription_job,
                TranscriptionJobName=job_name,
                Media={'MediaFileUri': media_uri},
                MediaFormat=media_format,
                LanguageCode='en-US'
            )
            job_started = True
            
            # 3. Wait for Job Completion
            deadline = asyncio.get_running_loop().time() + settings.AWS_TRANSCRIBE_TIMEOUT_SECONDS
            status_response = None
            while True:
                status_response = await asyncio.to_thread(
                    self.transcribe.get_transcription_job,
                    TranscriptionJobName=job_name
                )
                job_status = status_response['TranscriptionJob']['TranscriptionJobStatus']
                if job_status in ['COMPLETED', 'FAILED']:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(f"Transcribe job {job_name} timed out")
                await asyncio.sleep(5)
            
            if job_status == 'FAILED':
                raise Exception(f"Transcribe job {job_name} failed")
            
            # 4. Get Transcript
            transcript_uri = status_response['TranscriptionJob']['Transcript']['TranscriptFileUri']
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(transcript_uri)
                res.raise_for_status()
                res_json = res.json()
                raw_transcript = res_json['results']['transcripts'][0]['transcript']
                # Handle empty or whitespace-only audio
                transcript_text = raw_transcript.strip() if raw_transcript.strip() else "(No audible response provided)"
            
            # 5. Update DB History with Row Locking to prevent race conditions
            async with db_factory() as db:
                # SELECT FOR UPDATE locks this row so other background tasks wait
                stmt = select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
                res = await db.execute(stmt)
                session = res.scalar_one_or_none()
                
                if session:
                    # Make a copy to avoid mutation issues before commit
                    history = list(session.history or [])
                    
                    # Ensure history has enough space
                    while len(history) <= question_index:
                        history.append(None)
                    
                    history[question_index] = {
                        "question": question_text,
                        "answer_text": transcript_text,
                        "status": "completed",
                        "timestamp": str(uuid.uuid4())
                    }
                    
                    # Explicitly assign to trigger SQLAlchemy change detection
                    session.history = history
                    session.processing_status = "completed" if len(history) == len(session.questions or []) else "idle"
                    await db.commit()
                    logger.info(f"STT complete and history updated for session {session_id}, Q{question_index}")
                else:
                    logger.warning(f"Session {session_id} not found during history update")
            
        except Exception as e:
            logger.error(f"STT Error for session {session_id}, Q{question_index}: {e}", exc_info=True)
            # Optional: Mark this question as failed in history so generate_result doesn't wait forever
            try:
                async with db_factory() as db:
                    stmt = select(InterviewSession).where(InterviewSession.id == session_id).with_for_update()
                    res = await db.execute(stmt)
                    session = res.scalar_one_or_none()
                    if session:
                        history = list(session.history or [])
                        while len(history) <= question_index: history.append(None)
                        history[question_index] = {
                            "question": question_text,
                            "answer_text": "[STT Failed]",
                            "status": "failed",
                            "error": "Audio transcription failed. Please try again."
                        }
                        session.history = history
                        session.processing_status = "failed"
                        await db.commit()
            except Exception as inner_e:
                logger.error(f"Failed to mark STT error: {inner_e}")
        finally:
            if s3_key and self.s3 and settings.AWS_S3_BUCKET_NAME:
                try:
                    await asyncio.to_thread(
                        self.s3.delete_object,
                        Bucket=settings.AWS_S3_BUCKET_NAME,
                        Key=s3_key
                    )
                    logger.info("Deleted S3 audio object %s", s3_key)
                except Exception as cleanup_e:
                    logger.error(f"Failed to cleanup S3 object for {job_name}: {cleanup_e}")
            if job_started and self.transcribe:
                try:
                    await asyncio.to_thread(
                        self.transcribe.delete_transcription_job,
                        TranscriptionJobName=job_name
                    )
                    logger.info("Deleted Transcribe job %s", job_name)
                except Exception as cleanup_e:
                    logger.error(f"Failed to cleanup Transcribe job {job_name}: {cleanup_e}")
            # Cleanup local file
            if os.path.exists(audio_path):
                os.remove(audio_path)

    async def generate_result(self, db: AsyncSession, session_id: str) -> dict:
        """Generates report using transcribed text from history."""
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
        
        history = session.history or []
        questions = session.questions or []
        transcripts_complete = (
            len(history) == len(questions)
            and all(
                item is not None and not item.get("error") and item.get("answer_text")
                for item in history
            )
        )
        if not transcripts_complete:
            raise ValueError("Interview is still processing. Please try again shortly.")

        history_json = json.dumps(session.history, indent=2)

        prompt = AUDIO_INTERVIEW_REPORT_PROMPT.format(
            resume_text=session.resume_text,
            job_description=session.job_description,
            questions_json=history_json # Now sending the Q&A text history
        )
        
        try:
            content_text = await self._generate_content_with_fallback(
                prompt=prompt,
                response_mime_type="application/json"
            )
            session.is_active = False # Deactivate after completion
            raw_result = json.loads(content_text)
            
            sanitized = {
                "communication_score": raw_result.get("communication_score") or 0,
                "technical_knowledge_score": raw_result.get("technical_knowledge_score") or 0,
                "problem_solving_score": raw_result.get("problem_solving_score") or 0,
                "confidence_score": raw_result.get("confidence_score") or 0,
                "strengths": raw_result.get("strengths") or [],
                "weaknesses": raw_result.get("weaknesses") or [],
                "final_verdict": raw_result.get("final_verdict") or "Needs Improvement",
                "feedback_summary": raw_result.get("feedback_summary") or ""
            }
            
            suggestions = raw_result.get("improvement_suggestions") or []
            sanitized["improvement_suggestions"] = [suggestions] if isinstance(suggestions, str) else suggestions
                
            return sanitized

        except Exception as e:
            logger.error("!!! LLM CALL FAILED [generate_audio_result]: %s", e, exc_info=True)
            raise ValueError("Failed to generate valid report JSON")
