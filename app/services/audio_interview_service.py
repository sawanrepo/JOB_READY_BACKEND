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
from sqlalchemy import select, update, delete
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.prompts import AUDIO_INTERVIEW_QUESTIONS_PROMPT, AUDIO_INTERVIEW_REPORT_PROMPT
from app.models.interview import InterviewSession

logger = logging.getLogger(__name__)

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
        raise HTTPException(status_code=500, detail=f"AI service error: {err_str}")

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

        session_id = os.urandom(4).hex()

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
            history=[] # Initialize empty history
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
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Session not found")

        # Server-Side Anti-Tamper Time Limit Validation (3 mins answering + 2 mins upload grace. Adds +1 min if retake was claimed.)
        max_allowed = 360.0 if retaken else 300.0
        now = datetime.now(timezone.utc)
        served_at = session.updated_at
        if served_at.tzinfo is None:
            served_at = served_at.replace(tzinfo=timezone.utc)
        
        elapsed_seconds = (now - served_at).total_seconds()
        if elapsed_seconds > max_allowed:
            raise ValueError(
                f"Security alert: Answer submission timed out. You took {elapsed_seconds:.0f} seconds, which exceeds the maximum allowed time of {max_allowed:.0f} seconds."
            )

        # Update progress
        session.question_number = question_index + 1
        await db.commit()
        
        # Return necessary data for background task
        return {
            "session_id": session_id,
            "question_text": session.questions[question_index],
            "audio_path": audio_path,
            "question_index": question_index
        }

    async def process_stt_background(self, session_id: str, question_index: int, question_text: str, audio_path: str, db_factory):
        """Background task to handle S3 upload and AWS Transcribe."""
        if not self.s3 or not self.transcribe:
            logger.error(f"AWS clients not initialized. Skipping STT for session {session_id}")
            return

        job_name = f"stt_{session_id}_{question_index}_{uuid.uuid4().hex[:6]}"
        s3_key = f"audio_interviews/{session_id}/q_{question_index}.webm"
        
        try:
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
                MediaFormat='webm',
                LanguageCode='en-US'
            )
            
            # 3. Wait for Job Completion
            while True:
                status_response = await asyncio.to_thread(
                    self.transcribe.get_transcription_job,
                    TranscriptionJobName=job_name
                )
                job_status = status_response['TranscriptionJob']['TranscriptionJobStatus']
                if job_status in ['COMPLETED', 'FAILED']:
                    break
                await asyncio.sleep(5)
            
            if job_status == 'FAILED':
                raise Exception(f"Transcribe job {job_name} failed")
            
            # 4. Get Transcript
            transcript_uri = status_response['TranscriptionJob']['Transcript']['TranscriptFileUri']
            async with httpx.AsyncClient() as client:
                res = await client.get(transcript_uri)
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
                        "timestamp": str(uuid.uuid4())
                    }
                    
                    # Explicitly assign to trigger SQLAlchemy change detection
                    session.history = history
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
                        history[question_index] = {"question": question_text, "answer_text": "[STT Failed]", "error": str(e)}
                        session.history = history
                        await db.commit()
            except Exception as inner_e:
                logger.error(f"Failed to mark STT error: {inner_e}")

            # Cleanup S3 and Transcribe to prevent resource leaks
            try:
                await asyncio.to_thread(
                    self.s3.delete_object,
                    Bucket=settings.AWS_S3_BUCKET_NAME,
                    Key=s3_key
                )
                await asyncio.to_thread(
                    self.transcribe.delete_transcription_job,
                    TranscriptionJobName=job_name
                )
                logger.info(f"Cleaned up S3 and Transcribe for {job_name}")
            except Exception as cleanup_e:
                logger.error(f"Failed to cleanup S3/Transcribe for {job_name}: {cleanup_e}")

        except Exception as e:
            logger.error(f"STT Error for session {session_id}, Q{question_index}: {e}", exc_info=True)
        finally:
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
        
        # Wait for any pending STT jobs (simplified: just check if history length matches questions)
        # In a production app, we might want a more robust way to track pending jobs.
        # Wait for any pending STT jobs
        # We consider a job "pending" if the history index is None or if it's explicitly marked as an error.
        max_retries = 10 # ~50 seconds max wait
        while True:
            history = session.history or []
            questions = session.questions or []
            
            # Count entries that are either results or errors
            completed_count = len([h for h in history if h is not None])
            
            if completed_count >= len(questions) or max_retries <= 0:
                break
                
            logger.info(f"Waiting for STT jobs... {completed_count}/{len(questions)} (Retries left: {max_retries})")
            await asyncio.sleep(5)
            await db.refresh(session)
            max_retries -= 1

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
