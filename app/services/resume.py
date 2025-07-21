from app.utils.file import extract_text_from_pdf
from app.utils.latex import render_latex_template, compile_latex_to_pdf
from app.services.gemini import GeminiService
from fastapi import UploadFile, HTTPException
from app.prompts import ATS_ANALYSIS_PROMPT
from langchain.schema import SystemMessage, HumanMessage
import os

gemini_service = GeminiService()

async def run_analyze_resume(resume_text: str, job_description: str) -> dict:
    import json
    import traceback
    from pprint import pprint

    prompt = ATS_ANALYSIS_PROMPT.format(
        resume_text=resume_text,
        job_description=job_description
    )

    messages = [
        SystemMessage(content="You are an expert resume analyst."),
        HumanMessage(content=prompt)
    ]

    print("\n📨 Sending prompt to Gemini...\n")
    try:
        response = await gemini_service.llm.agenerate([messages])
        content = response.generations[0][0].text
        print("\n📩 Gemini raw response:\n", content)

        # Clean code block markers
        if content.startswith("```json"):
            content = content[7:-3].strip()

        # Try parsing JSON
        parsed = json.loads(content)
        print("\n✅ Parsed response:")
        pprint(parsed)

        return parsed

    except Exception as e:
        print("\n❌ Gemini or JSON parsing failed:")
        traceback.print_exc()
        raise e


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