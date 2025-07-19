ATS_ANALYSIS_PROMPT = """
You are an expert ATS (Applicant Tracking System) scanner and resume analyst. 
Analyze the provided resume and job description. Provide the following in JSON format:

{{
  "ats_score": <score out of 100>,
  "matched_skills": [list of skills from the resume that match the job description],
  "missing_skills": [list of skills required by the job description that are missing from the resume],
  "improvement_suggestions": [list of actionable suggestions to improve the resume for this job]
}}

Be critical and constructive. The score should be based on the relevance of the resume to the job description.

RESUME:
{resume_text}

JOB DESCRIPTION:
{job_description}
"""

TAILOR_RESUME_PROMPT = """
You are a professional resume writer. Tailor the provided resume to the job description. 
Focus on:
- Highlighting relevant skills and experiences
- Using keywords from the job description
- Adjusting the summary/objective to align with the job
- Reordering sections to prioritize relevant information

Return ONLY the tailored resume content in plain text. Do not include any explanations or markdown formatting.

RESUME:
{resume_text}

JOB DESCRIPTION:
{job_description}
"""