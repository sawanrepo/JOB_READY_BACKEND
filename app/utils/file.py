import pdfplumber
import aiofiles
import os
import uuid
from fastapi import UploadFile

import asyncio

async def extract_text_from_pdf(upload_file: UploadFile) -> str:
    import traceback

    temp_filename = f"temp_{uuid.uuid4()}.pdf"
    try:
        # Save uploaded PDF to disk
        async with aiofiles.open(temp_filename, 'wb') as out_file:
            content = await upload_file.read()
            await out_file.write(content)

        def dump_text():
            with pdfplumber.open(temp_filename) as pdf:
                if len(pdf.pages) > 3:
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=400,
                        detail="PDF is too long to be a valid resume. Please upload a resume of 1 or 2 pages."
                    )
                text = ""
                for page in pdf.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"
                return text

        text = await asyncio.to_thread(dump_text)
        if not text:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400,
                detail="The uploaded PDF is not compatible with our ATS checker. Either the uploaded PDF is image-based or doesn't have text that is valid for a general ATS check. Please use a text-based (Lex-based) resume for better results."
            )
        return text

    except Exception as e:
        traceback.print_exc()
        raise e

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)