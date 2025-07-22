from app.utils.file import extract_text_from_pdf
from app.utils.latex import render_latex_template, compile_latex_to_pdf
from app.services.gemini import GeminiService
from fastapi import UploadFile, HTTPException
import os

gemini_service = GeminiService()

async def run_analyze_resume(resume_text: str, job_description: str) -> dict:
    try:
        analysis_result = await gemini_service.analyze_resume(resume_text, job_description)
        return analysis_result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Resume analysis failed: {str(e)}"
        )

async def tailor_resume(resume_pdf: UploadFile, job_description: str) -> dict:
    try:
        print("[DEBUG] Starting resume tailoring process...")
        resume_text = await extract_text_from_pdf(resume_pdf)
        print("[DEBUG] Extracted resume text. send to Gemini for tailoring...")
        tailored_content = await gemini_service.tailor_resume(resume_text, job_description)
        print("[DEBUG] Tailored resume content received.",list(tailored_content.keys()))  # Log first 100 chars for debugging
        
        # Generate LaTeX and compile to PDF
        print("[DEBUG] Rendering LaTeX template...")
        latex_content = render_latex_template(tailored_content)
        print("[DEBUG] Compiling LaTeX to PDF...")
        pdf_path = compile_latex_to_pdf(latex_content)
        print("[DEBUG] PDF compiled successfully at:", pdf_path)
        filename = os.path.basename(pdf_path)
        
        return {
            "tailored_content": tailored_content,
            "filename": filename,
            "pdf_url": f"/download/{os.path.basename(pdf_path)}"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Resume tailoring failed: {str(e)}"
        )