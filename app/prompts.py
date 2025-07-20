ATS_ANALYSIS_PROMPT = """
You are an expert ATS (Applicant Tracking System) scanner and resume analyst.
Analyze the provided resume and job description for ATS compatibility and recruiter appeal.

Return a JSON response with the following fields:

{
  "ats_score": <score out of 100>,
  "keyword_match_percentage": <percentage>,
  "matched_skills": [...],
  "missing_skills": [...],
  "job_title_match": "<Yes/No with explanation>",
  "experience_alignment": "<Brief summary of how experience matches JD>",
  "education_alignment": "<Brief summary of alignment with JD requirements>",
  "resume_format_compliance": [list any format issues like missing sections, bad structure],
  "action_verbs_count": <number of action verbs detected>,
  "red_flags": [e.g., employment gaps, vague statements, missing contact info],
  "improvement_suggestions": [concrete suggestions for tailoring the resume]
}

Be strict but constructive. Score based on how well the resume matches the job description in terms of skills, experience, structure, and ATS readability.

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