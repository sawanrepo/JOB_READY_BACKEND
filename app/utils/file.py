import pdfplumber
import aiofiles
import os
import uuid
from fastapi import UploadFile

async def extract_text_from_pdf(upload_file: UploadFile) -> str:
    import traceback

    temp_filename = f"temp_{uuid.uuid4()}.pdf"
    try:
        # Save uploaded PDF to disk
        async with aiofiles.open(temp_filename, 'wb') as out_file:
            content = await upload_file.read()
            await out_file.write(content)

        text = ""
        with pdfplumber.open(temp_filename) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        return text or "No extractable text found in PDF."

    except Exception as e:
        traceback.print_exc()
        raise e

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)