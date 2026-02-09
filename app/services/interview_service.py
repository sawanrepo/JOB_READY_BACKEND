import os
from google import genai
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.prompts import INTERVIEW_SYSTEM_PROMPT, INTERVIEW_ANALYSIS_PROMPT, INTERVIEW_REPORT_PROMPT
import json
import logging
import asyncio

logger = logging.getLogger(__name__)

# Simple in-memory storage for MVP
interview_sessions = {}

class InterviewService:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)

    async def start_interview(self, request: InterviewStartRequest) -> InterviewResponse:
        session_id = os.urandom(4).hex()
        
        # Initialize session state
        interview_sessions[session_id] = {
            "resume_text": request.resume_text,
            "job_description": request.job_description,
            "history": [], # List of {question, response_analysis}
            "question_number": 0,
            "current_question":  None
        }
        
        # Generate first question
        logger.info(f"Starting interview session {session_id}")
        question = await self._generate_next_question(session_id)
        
        return InterviewResponse(
            session_id=session_id,
            question=question,
            question_number=1,
            total_questions=8
        )

    async def process_response(self, session_id: str, video_path: str) -> InterviewResponse:
        if session_id not in interview_sessions:
            raise ValueError("Invalid session ID")
        
        session = interview_sessions[session_id]
        current_q = session["current_question"]

        logger.info(f"Processing response for session {session_id}, question {session['question_number']}")

        # 1. Analyze video response
        analysis = await self._analyze_video(video_path, current_q)
        
        # 2. Update history
        session["history"].append({
            "question": current_q,
            "analysis": analysis
        })
        
        # 3. Generate next question
        next_q = await self._generate_next_question(session_id)
        
        ended = next_q == "INTERVIEW_END"
        
        if ended:
             logger.info(f"Interview ended for session {session_id}")

        return InterviewResponse(
            session_id=session_id,
            question=next_q if not ended else "",
            question_number=session["question_number"],
            total_questions=8,
            interview_ended=ended
        )

    async def _generate_next_question(self, session_id: str) -> str:
        session = interview_sessions[session_id]
        session["question_number"] += 1
        
        if session["question_number"] > 8:
            return "INTERVIEW_END"

        # Construct prompt
        history_text = "\n".join([f"Q: {h['question']}\nSearch: {h['analysis']}" for h in session["history"]])
        prompt = INTERVIEW_SYSTEM_PROMPT.format(
            question_number=session["question_number"],
            total_questions=8,
            last_question=session["current_question"] if session["current_question"] else "None (Start of Interview)",
            resume_summary=session["resume_text"][:800] + "..." if len(session["resume_text"]) > 800 else session["resume_text"],
            jd_summary=session["job_description"][:800] + "..." if len(session["job_description"]) > 800 else session["job_description"],
            history=history_text if history_text else "None"
        )
        
        response = await self.client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        next_q = response.text.strip()
        
        session["current_question"] = next_q
        return next_q

    async def _analyze_video(self, video_path: str, question: str) -> str:
        # Upload file using synchronous client as files.upload is typically fast for small files 
        # but better to run in executor or use aio if available.
        # Note: google-genai 1.x aio.files.upload might not be fully async in all versions, 
        # using run_in_executor for the file upload if needed, but keeping it direct for MVP.
        logger.info(f"Uploading video {video_path} to Gemini...")
        video_file = self.client.files.upload(file=video_path)
        
        # Wait for processing
        while True:
             file = self.client.files.get(name=video_file.name)
             if file.state != 'PROCESSING':
                 break
             logger.info("Waiting for video processing...")
             await asyncio.sleep(2)

        if file.state == 'FAILED':
             raise ValueError("Video processing failed by Gemini")

        logger.info("Video processed. Generating analysis...")
        prompt = INTERVIEW_ANALYSIS_PROMPT.format(question=question)
        response = await self.client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, file]
        )
        return response.text

    async def generate_result(self, session_id: str) -> InterviewResult:
        if session_id not in interview_sessions:
            raise ValueError("Invalid session ID")

        session = interview_sessions[session_id]
        
        history_text = "\n".join([f"Q: {h['question']}\nAnalysis: {h['analysis']}" for h in session["history"]])
        
        prompt = INTERVIEW_REPORT_PROMPT.format(
            resume_text=session["resume_text"],
            job_description=session["job_description"],
            history=history_text
        )
        
        response = await self.client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        content = response.text
        
        # Cleanup
        try:
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()
                
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse report JSON: {e}")
            # Fallback or retry logic could go here
            raise ValueError("Failed to generate valid report JSON")
