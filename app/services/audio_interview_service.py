import os
import json
import logging
import asyncio
import uuid
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
            logger.info(">>> LLM CALL START [start_audio_interview] | Model: %s", self.model_id)
            response = await self.client.aio.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.8)
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

    async def save_audio_answer(self, db: AsyncSession, session_id: str, question_index: int, audio_path: str):
        """Saves audio answer metadata and triggers background STT."""
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Session not found")

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
                transcript_text = res_json['results']['transcripts'][0]['transcript']
            
            # 5. Update DB History
            async with db_factory() as db:
                stmt = select(InterviewSession).where(InterviewSession.id == session_id)
                res = await db.execute(stmt)
                session = res.scalar_one_or_none()
                if session:
                    history = list(session.history or [])
                    # Ensure history has enough space (fill gaps if needed)
                    while len(history) <= question_index:
                        history.append(None)
                    
                    history[question_index] = {
                        "question": question_text,
                        "answer_text": transcript_text,
                        "timestamp": str(uuid.uuid4()) # For uniqueness
                    }
                    session.history = history
                    await db.commit()
            
            logger.info(f"STT complete for session {session_id}, Q{question_index}")
            
            # 6. Cleanup S3 and Transcribe Job
            try:
                logger.info(f"Cleaning up S3 and Transcribe job for {job_name}")
                await asyncio.to_thread(
                    self.s3.delete_object,
                    Bucket=settings.AWS_S3_BUCKET_NAME,
                    Key=s3_key
                )
                await asyncio.to_thread(
                    self.transcribe.delete_transcription_job,
                    TranscriptionJobName=job_name
                )
            except Exception as cleanup_err:
                logger.warning(f"Cleanup failed for STT job {job_name}: {cleanup_err}")

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
        max_retries = 12 # 1 minute max wait
        while len([h for h in (session.history or []) if h is not None]) < len(session.questions or []):
            if max_retries <= 0:
                break
            logger.info(f"Waiting for STT jobs to complete... {len(session.history)}/10")
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
            response = await self.client.aio.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            session.is_active = False # Deactivate after completion
            raw_result = json.loads(response.text)
            
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
