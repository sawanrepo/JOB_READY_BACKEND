import pdfplumber
import aiofiles
import os
import uuid
from fastapi import UploadFile

async def extract_text_from_pdf(upload_file: UploadFile) -> str:
    temp_filename = f"temp_{uuid.uuid4()}.pdf"
    
    async with aiofiles.open(temp_filename, 'wb') as out_file:
        content = await upload_file.read()
        await out_file.write(content)
    
    text = ""
    try:
        with pdfplumber.open(temp_filename) as pdf:
            for page in pdf.pages:
                text += page.extract_text() + "\n"
    finally:
        os.remove(temp_filename)
    
    return text