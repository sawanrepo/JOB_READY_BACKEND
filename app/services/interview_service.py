import os
import google.generativeai as genai
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
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-2.5-flash')

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
        history_text = "\n".join([f"Q: {h['question']}\nAnalysis: {h['analysis']}" for h in session["history"]])
        prompt = INTERVIEW_SYSTEM_PROMPT.format(
            question_number=session["question_number"],
            total_questions=8,
            last_question=session["current_question"] if session["current_question"] else "None (Start of Interview)",
            resume_summary=session["resume_text"][:800] + "..." if len(session["resume_text"]) > 800 else session["resume_text"],
            jd_summary=session["job_description"][:800] + "..." if len(session["job_description"]) > 800 else session["job_description"],
            history=history_text if history_text else "None"
        )
        
        try:
            logger.info(">>> LLM CALL START [_generate_next_question] | Model: %s | Prompt chars: %d", 'gemini-2.5-flash', len(prompt))
            response = await self.model.generate_content_async(
                prompt,
                generation_config={"temperature": 0.8}
            )
            logger.info("<<< LLM CALL SUCCESS [_generate_next_question]")

        except Exception as e:
            logger.error("!!! LLM CALL FAILED [_generate_next_question]: %s", e, exc_info=True)
            raise
        next_q = response.text.strip()
        
        session["current_question"] = next_q
        return next_q

    async def _analyze_video(self, video_path: str, question: str) -> str:
        # Upload file using thread pool to avoid blocking the event loop
        logger.info(f"Uploading video {video_path} to Gemini...")
        video_file = await asyncio.to_thread(genai.upload_file, path=video_path)
        
        # Wait for processing
        while True:
             file_info = await asyncio.to_thread(genai.get_file, video_file.name)
             if file_info.state.name != 'PROCESSING':
                 break
             logger.info("Waiting for video processing...")
             await asyncio.sleep(2)

        if file_info.state.name == 'FAILED':
             raise ValueError("Video processing failed by Gemini")

        logger.info("Video processed. Generating analysis...")
        prompt = INTERVIEW_ANALYSIS_PROMPT.format(question=question)
        logger.info(">>> LLM CALL START [_analyze_video] | Model: %s | Prompt chars: %d", 'gemini-2.5-flash', len(prompt))
        try:
            response = await self.model.generate_content_async(
                contents=[prompt, video_file]
            )
            logger.info("<<< LLM CALL SUCCESS [_analyze_video]")
            return response.text
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [_analyze_video]: %s", e, exc_info=True)
            raise

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
        
        logger.info(">>> LLM CALL START [generate_result] | Model: %s | Prompt chars: %d", 'gemini-2.5-flash', len(prompt))
        try:
            generation_config = {"response_mime_type": "application/json"}
            response = await self.model.generate_content_async(
                contents=prompt,
                generation_config=generation_config
            )
            logger.info("<<< LLM CALL SUCCESS [generate_result]")
            content = response.text
            return json.loads(content)
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [generate_result]: %s", e, exc_info=True)
            raise ValueError("Failed to generate valid report JSON")
