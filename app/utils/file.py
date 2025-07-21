import pdfplumber
import aiofiles
import os
import uuid
from fastapi import UploadFile

async def extract_text_from_pdf(upload_file: UploadFile) -> str:
    import traceback

    temp_filename = f"temp_{uuid.uuid4()}.pdf"
    print(f"📄 Saving uploaded PDF to temp file: {temp_filename}")

    try:
        # Save uploaded PDF to disk
        async with aiofiles.open(temp_filename, 'wb') as out_file:
            content = await upload_file.read()
            await out_file.write(content)
        print(f"✅ File saved. Size: {len(content)} bytes")

        # Extract text with pdfplumber
        text = ""
        with pdfplumber.open(temp_filename) as pdf:
            print(f"📑 Total pages: {len(pdf.pages)}")
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"

        print("📜 Extracted text sample:")
        print(text[:300])

        return text or "No extractable text found in PDF."

    except Exception as e:
        print("❌ PDF extraction failed!")
        traceback.print_exc()
        raise e

    finally:
        # Always clean up temp file
        if os.path.exists(temp_filename):
            os.remove(temp_filename)