ATS_ANALYSIS_PROMPT = """
You are an expert ATS (Applicant Tracking System) scanner and resume analyst.
Analyze the provided resume and job description for ATS compatibility and recruiter appeal.

Return a JSON response with the following fields:

{{
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
}}

Be strict but constructive. Score based on how well the resume matches the job description in terms of skills, experience, structure, and ATS readability.
You may ignore exact dates of internships or education. However, still evaluate overall educational qualifications and relevance to the job description.
Don't Include career Gap as red flag if applying for internship or entry-level positions.
u dont have access to current date and time so avoid that in your analysis
RESUME:
{resume_text}

JOB DESCRIPTION:
{job_description}
"""

TAILOR_RESUME_PROMPT = """
You are a professional resume writing assistant. Your task is to tailor the candidate's resume to match the given job description.

Optimize for:
- Rewriting the **summary** to align with the role
- Highlighting **relevant skills, projects, and experiences**
- Using **keywords** and phrases found in the job description
- Adjusting **order** of sections to emphasize relevant content
- Omitting irrelevant or weak details

Respond with a **structured JSON object** with the following fields. Return only valid JSON without markdown, code fences, or explanations.

Expected format:
{{
  "name": "Full Name",
  "tagline": "One-line title",
  "personal_info": {{
    "location": "",
    "phone": "",
    "email": "",
    "github": "",
    "github_url": "",
    "linkedin": "",
    "linkedin_url": "",
    "portfolio": "",
    "portfolio_url": ""
  }},
  "summary": "Updated and targeted summary paragraph.",
  "skills": {{
    <"key">: ["skill1", "skill2", ...],
    "Languages": [],
    "Frameworks": [],
    you can add more categories as needed with relevant skills.
  }},
  "experience": [
    {{
      "role": "",
      "company": "",
      "duration": "",
      "details": ["Point 1", "Point 2"]
    }}
  ],
  "projects": [
    {{
      "title": "",
      "tech_stack": "",
      "details": ["Point 1", "Point 2"]
    }}
  ],
  "education": [
    {{
      "degree": "",
      "institution": "",
      "grade": "", # Optional
      "start_date": "",
      "end_date": ""
    }}
  ],
  "certifications": [
    {{
      "title": "",
      "link": "",
      "date": ""
    }}
  ]
}}

RESUME:
{resume_text}

JOB DESCRIPTION:
{job_description}
"""