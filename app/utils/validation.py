import re
from fastapi import HTTPException

def validate_resume_text(text: str):
    """
    Checks extracted resume text for basic quality.
    (Prompt injection is now handled via delimiter sandboxing in system prompts)
    """
    if not text or len(text.strip()) < 50:
        raise HTTPException(status_code=400, detail="The extracted resume text is too short or invalid.")
    return True

def validate_job_description(text: str):
    """
    Validates the job description to prevent abuse and ensure quality.
    """
    if not text:
        raise HTTPException(status_code=400, detail="Job description is required.")

    char_count = len(text)
    
    # 1. Length Check
    if char_count < 100:
        raise HTTPException(status_code=400, detail="Job description too short. Please provide a full description (min 100 chars).")
    if char_count > 8000:
        raise HTTPException(status_code=400, detail="Job description too long. Maximum 8000 characters allowed.")

    # 2. Word Count Check
    words = text.split()
    if len(words) < 30:
        raise HTTPException(status_code=400, detail="Job description too brief. Please provide a detailed description (min 30 words).")

    # 3. Repeated Characters Check (e.g., "aaaaaaa")
    if re.search(r'(.)\1{5,}', text):
        raise HTTPException(status_code=400, detail="Job description contains too many repeated characters.")

    # 4. Natural Language Check (Alphanumeric vs Symbols)
    alnum_count = sum(c.isalnum() for c in text)
    symbol_count = char_count - alnum_count - text.count(' ')
    
    if char_count > 0:
        alnum_ratio = alnum_count / char_count
        symbol_ratio = symbol_count / char_count
        
        if symbol_ratio > 0.2:
            raise HTTPException(status_code=400, detail="Job description contains too many symbols.")
        if alnum_ratio < 0.6:
            raise HTTPException(status_code=400, detail="Job description contains too little natural language.")

    return True
