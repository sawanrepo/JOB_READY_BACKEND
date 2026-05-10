import os
import json
import logging
import asyncio
import google.generativeai as genai
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResult
from app.prompts import AUDIO_INTERVIEW_QUESTIONS_PROMPT, AUDIO_INTERVIEW_REPORT_PROMPT

logger = logging.getLogger(__name__)

# Simple in-memory storage for MVP
audio_interview_sessions = {}

class AudioInterviewService:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-2.5-flash')

    async def start_audio_interview(self, request: InterviewStartRequest) -> dict:
        session_id = os.urandom(4).hex()
        
        # 1. Generate 10 questions at once
        prompt = AUDIO_INTERVIEW_QUESTIONS_PROMPT.format(
            resume_text=request.resume_text[:1000] + "..." if len(request.resume_text) > 1000 else request.resume_text,
            job_description=request.job_description[:1000] + "..." if len(request.job_description) > 1000 else request.job_description
        )
        
        try:
            logger.info(">>> LLM CALL START [start_audio_interview] | Model: gemini-2.5-flash")
            response = await self.model.generate_content_async(prompt)
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

        audio_interview_sessions[session_id] = {
            "resume_text": request.resume_text,
            "job_description": request.job_description,
            "questions": questions
        }
        
        return {
            "session_id": session_id,
            "questions": questions,
            "total_questions": len(questions)
        }

    async def generate_result(self, session_id: str, audio_path: str) -> dict:
        if session_id not in audio_interview_sessions:
            raise ValueError("Invalid session ID")

        session = audio_interview_sessions[session_id]
        
        logger.info(f"Uploading merged audio {audio_path} to Gemini...")
        audio_file = await asyncio.to_thread(
            genai.upload_file, 
            path=audio_path,
            mime_type='audio/webm'
        )
        
        # Wait for processing
        while True:
             file_info = await asyncio.to_thread(genai.get_file, audio_file.name)
             if file_info.state.name != 'PROCESSING':
                 break
             logger.info("Waiting for audio processing...")
             await asyncio.sleep(2)

        if file_info.state.name == 'FAILED':
             raise ValueError("Audio processing failed by Gemini")

        logger.info("Audio processed. Generating final report...")

        prompt = AUDIO_INTERVIEW_REPORT_PROMPT.format(
            resume_text=session["resume_text"],
            job_description=session["job_description"],
            questions_json=json.dumps(session["questions"], indent=2)
        )
        
        try:
            logger.info(">>> LLM CALL START [generate_audio_result] | Model: gemini-2.5-flash")
            
            # Using generation config for JSON response
            generation_config = {"response_mime_type": "application/json"}
            response = await self.model.generate_content_async(
                contents=[prompt, audio_file],
                generation_config=generation_config
            )
            logger.info("<<< LLM CALL SUCCESS [generate_audio_result]")
            content = response.text
            return json.loads(content)
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [generate_audio_result]: %s", e, exc_info=True)
            raise ValueError("Failed to generate valid report JSON")
