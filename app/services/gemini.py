from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage, SystemMessage
import json
from app.config import settings
from app.prompts import ATS_ANALYSIS_PROMPT, TAILOR_RESUME_PROMPT
from fastapi import HTTPException
import re

class GeminiService:
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.GEMINI_API_KEY,
            temperature=0.3
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

        response = await self.llm.agenerate([messages])
        content = response.generations[0][0].text

        try:
            # Handle Gemini's markdown-style JSON output
            if content.startswith("```json"):
                content = content[7:-3].strip()
            return json.loads(content)

        except json.JSONDecodeError:
            # Fallback: Extract JSON from text
            start = content.find('{')
            end = content.rfind('}') + 1
            return json.loads(content[start:end])
    
    async def tailor_resume(self, resume_text: str, job_description: str) -> dict:
        print("[INFO] Building prompt for Gemini...")
        prompt = TAILOR_RESUME_PROMPT.format(
            resume_text=resume_text,
            job_description=job_description
        )
        
        messages = [
            SystemMessage(content="You are a professional resume writer."),
            HumanMessage(content=prompt)
        ]
        print("[INFO] Sending to Gemini...")
        
        response = await self.llm.agenerate([messages])
        content =  response.generations[0][0].text
        print("[DEBUG] Gemini raw output:")
        print(content[:100] + "...")
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = re.sub(r'^```.*\n', '', content, flags=re.DOTALL)
            content = re.sub(r'\n```$', '', content)
        try:
            parased = json.loads(content)
            print("[DEBUG] Gemini response parsed successfully.")
            return parased
        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to parse Gemini response: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse Gemini response. \nError: {str(e)} \n \n raw response: {content}"
            )
        