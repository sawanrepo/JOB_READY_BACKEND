import re
from fastapi import HTTPException

MIN_FULL_JOB_DESCRIPTION_WORDS = 30
MAX_JOB_INPUT_CHARS = 8000
LIMITED_JOB_INPUT_NOTE = (
    "This result is based only on the job role or limited input, not a full job "
    "description. For more accurate results, paste the exact job description."
)


def get_word_count(text: str) -> int:
    return len((text or "").split())


def get_job_input_note(text: str) -> str | None:
    if get_word_count(text) < MIN_FULL_JOB_DESCRIPTION_WORDS:
        return LIMITED_JOB_INPUT_NOTE
    return None


def get_job_input_type(text: str) -> str:
    return "limited_job_input" if get_job_input_note(text) else "full_job_description"


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
    if char_count > MAX_JOB_INPUT_CHARS:
        raise HTTPException(status_code=400, detail="Job description too long. Maximum 8000 characters allowed.")

    # 2. Word Count Check
    words = text.split()
    if len(words) < MIN_FULL_JOB_DESCRIPTION_WORDS:
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


def validate_job_target(text: str):
    """
    Validates ATS/tailor target input.
    Allows a short role/title while keeping abuse and quality checks.
    """
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Job description or job role is required.")

    text = text.strip()
    char_count = len(text)

    if char_count > MAX_JOB_INPUT_CHARS:
        raise HTTPException(status_code=400, detail="Job input too long. Maximum 8000 characters allowed.")

    if re.search(r'(.)\1{5,}', text):
        raise HTTPException(status_code=400, detail="Job input contains too many repeated characters.")

    alnum_count = sum(c.isalnum() for c in text)
    symbol_count = char_count - alnum_count - sum(c.isspace() for c in text)

    if char_count > 0:
        alnum_ratio = alnum_count / char_count
        symbol_ratio = symbol_count / char_count

        if symbol_ratio > 0.2:
            raise HTTPException(status_code=400, detail="Job input contains too many symbols.")
        if alnum_ratio < 0.6:
            raise HTTPException(status_code=400, detail="Job input contains too little natural language.")

    return True
