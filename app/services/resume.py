from app.utils.file import extract_text_from_pdf
from app.utils.latex import render_latex_template, compile_latex_to_pdf
from app.services.gemini import GeminiService
from fastapi import UploadFile, HTTPException
import os

gemini_service = GeminiService()

async def analyze_resume(resume_pdf: UploadFile, job_description: str) -> dict:
    try:
        resume_text = await extract_text_from_pdf(resume_pdf)
        return await gemini_service.analyze_resume(resume_text, job_description)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Resume analysis failed: {str(e)}"
        )

async def tailor_resume(resume_pdf: UploadFile, job_description: str) -> dict:
    try:
        resume_text = await extract_text_from_pdf(resume_pdf)
        tailored_content = await gemini_service.tailor_resume(resume_text, job_description)
        
        # Generate LaTeX and compile to PDF
        latex_content = render_latex_template(tailored_content)
        pdf_path = compile_latex_to_pdf(latex_content)
        
        return {
            "tailored_content": tailored_content,
            "pdf_url": f"/download/{os.path.basename(pdf_path)}"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Resume tailoring failed: {str(e)}"
        )