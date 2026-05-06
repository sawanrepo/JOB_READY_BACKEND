from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage, SystemMessage
import json
import logging
from app.config import settings
from app.prompts import ATS_ANALYSIS_PROMPT, TAILOR_RESUME_PROMPT
from fastapi import HTTPException
import re

logger = logging.getLogger(__name__)


def _raise_for_gemini_error(e: Exception) -> None:
    """Convert Gemini / Google API errors into clean HTTP exceptions."""
    err_str = str(e)
    # Rate-limit / quota exceeded (free tier: 5 req/min)
    if "ResourceExhausted" in type(e).__name__ or "429" in err_str or "quota" in err_str.lower():
        raise HTTPException(
            status_code=429,
            detail="AI service is temporarily rate-limited. Please wait a moment and try again."
        )
    # Service unavailable / overloaded
    if "ServiceUnavailable" in type(e).__name__ or "503" in err_str:
        raise HTTPException(
            status_code=503,
            detail="AI service is temporarily unavailable. Please try again shortly."
        )
    raise e  # re-raise anything else unchanged


class GeminiService:
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite-preview",
            google_api_key=settings.GEMINI_API_KEY,
            temperature=0.3,
            max_retries=3
        )

    async def analyze_resume(self, resume_text: str, job_description: str) -> dict:
        prompt = ATS_ANALYSIS_PROMPT.format(
            resume_text=resume_text,
            job_description=job_description
        )

        messages = [
            SystemMessage(content="You are an expert resume analyst."),
            HumanMessage(content=prompt)
        ]

        try:
            logger.info(">>> LLM CALL START [analyze_resume] | Model: %s | Prompt chars: %d", self.llm.model, len(prompt))
            response = await self.llm.agenerate([messages])
            logger.info("<<< LLM CALL SUCCESS [analyze_resume]")
        except HTTPException:
            raise
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [analyze_resume]: %s", e, exc_info=True)
            _raise_for_gemini_error(e)

        content = response.generations[0][0].text

        try:
            if content.startswith("```json"):
                content = content[7:-3].strip()
            return json.loads(content)
        except json.JSONDecodeError:
            # Fallback: extract JSON substring
            start = content.find('{')
            end = content.rfind('}') + 1
            return json.loads(content[start:end])

    async def tailor_resume(self, resume_text: str, job_description: str) -> dict:
        prompt = TAILOR_RESUME_PROMPT.format(
            resume_text=resume_text,
            job_description=job_description
        )

        messages = [
            SystemMessage(content="You are a professional resume writer."),
            HumanMessage(content=prompt)
        ]

        try:
            logger.info(">>> LLM CALL START [tailor_resume] | Model: %s | Prompt chars: %d", self.llm.model, len(prompt))
            response = await self.llm.agenerate([messages])
            logger.info("<<< LLM CALL SUCCESS [tailor_resume]")
        except HTTPException:
            raise
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [tailor_resume]: %s", e, exc_info=True)
            _raise_for_gemini_error(e)

        content = response.generations[0][0].text

        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = re.sub(r'^```.*\n', '', content, flags=re.DOTALL)
            content = re.sub(r'\n```$', '', content)

        try:
            parsed = json.loads(content)
            return parsed
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Gemini response: %s", e)
            raise HTTPException(
                status_code=500,
                detail="Failed to parse AI response. Please try again."
            )