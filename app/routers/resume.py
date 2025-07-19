from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from app.schemas.resume import ResumeAnalysisResponse, TailoredResumeResponse
from app.services.resume import analyze_resume, tailor_resume
from app.routers.auth import get_current_user
from app.models.user import User
import os
from fastapi.responses import FileResponse

router = APIRouter()

@router.post("/ats-check", response_model=ResumeAnalysisResponse)
async def ats_check(
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user)
):
    return await analyze_resume(resume_pdf, job_description)

@router.post("/tailor-resume", response_model=TailoredResumeResponse)
async def tailor_resume_endpoint(
    resume_pdf: UploadFile = File(...),
    job_description: str = Form(...),
    current_user: User = Depends(get_current_user)
):
    return await tailor_resume(resume_pdf, job_description)

@router.get("/download/{filename}")
async def download_resume(filename: str):
    file_path = f"output/{filename}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/pdf")