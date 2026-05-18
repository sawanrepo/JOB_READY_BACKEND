from app.utils.file import extract_text_from_pdf
from app.utils.latex import render_latex_template, compile_latex_to_pdf
from app.services.gemini import GeminiService
from fastapi import UploadFile, HTTPException
import asyncio
import os
import logging

logger = logging.getLogger(__name__)

gemini_service = GeminiService()


async def run_analyze_resume(resume_text: str, job_description: str) -> dict:
    try:
        return await gemini_service.analyze_resume(resume_text, job_description)
    except HTTPException:
        raise  # preserve 429/503 from Gemini layer
    except Exception as e:
        logger.error("run_analyze_resume error: %s", e)
        raise HTTPException(status_code=500, detail="Resume analysis failed. Please try again.")


async def tailor_resume(resume_pdf: UploadFile, job_description: str) -> dict:
    try:
        resume_text = await extract_text_from_pdf(resume_pdf)
        tailored_content = await gemini_service.tailor_resume(resume_text, job_description)

        if not tailored_content.get("is_resume", True):
            return {
                "is_resume": False,
                "error_message": tailored_content.get("error_message", "The uploaded PDF does not appear to be a valid resume."),
                "tailored_content": None,
                "filename": None,
                "pdf_url": None
            }

        latex_content = await asyncio.to_thread(render_latex_template, tailored_content)

        pdf_path = await asyncio.to_thread(compile_latex_to_pdf, latex_content, "output", tailored_content)
        filename = os.path.basename(pdf_path)

        return {
            "tailored_content": tailored_content,
            "filename": filename,
            "pdf_url": f"/download/{filename}"
        }
    except HTTPException:
        raise  # preserve 429/503 from Gemini layer
    except Exception as e:
        logger.error("tailor_resume error: %s", e)
        raise HTTPException(status_code=500, detail="Resume tailoring failed. Please try again.")

async def propose_tailor_resume(resume_text: str, job_description: str) -> dict:
    try:
        tailored_content = await gemini_service.tailor_resume(resume_text, job_description)
        return tailored_content
    except HTTPException:
        raise
    except Exception as e:
        logger.error("propose_tailor_resume error: %s", e)
        raise HTTPException(status_code=500, detail="Resume tailoring analysis failed. Please try again.")

async def generate_tailored_pdf(tailored_content: dict) -> dict:
    try:
        # Enforce absolute upper safety ceiling of 10 on backend for DoS protection
        if "experience" in tailored_content and isinstance(tailored_content["experience"], list):
            tailored_content["experience"] = tailored_content["experience"][:10]
        if "projects" in tailored_content and isinstance(tailored_content["projects"], list):
            tailored_content["projects"] = tailored_content["projects"][:10]

        latex_content = await asyncio.to_thread(render_latex_template, tailored_content)
        pdf_path = await asyncio.to_thread(compile_latex_to_pdf, latex_content, "output", tailored_content)
        filename = os.path.basename(pdf_path)

        return {
            "filename": filename,
            "pdf_url": f"/download/{filename}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("generate_tailored_pdf error: %s", e)
        raise HTTPException(status_code=500, detail="Resume tailoring generation failed. Please try again.")