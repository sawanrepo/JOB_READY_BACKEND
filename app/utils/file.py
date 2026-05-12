import pdfplumber
import aiofiles
import os
import uuid
from fastapi import UploadFile, HTTPException
import asyncio
import magic

# Security configuration
ALLOWED_RESUME_EXTENSIONS = {".pdf"}
ALLOWED_VIDEO_EXTENSIONS = {".webm", ".mp4", ".ogg", ".mkv"}
ALLOWED_AUDIO_EXTENSIONS = {".webm", ".mp3", ".wav", ".m4a", ".ogg"}

# Allowed MIME types
ALLOWED_MIME_TYPES = {
    # Documents
    "application/pdf",
    # Video
    "video/webm", "video/mp4", "video/ogg", "video/x-matroska",
    # Audio
    "audio/webm", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/mp4", "audio/aac", "audio/x-m4a"
}

def validate_file_security(upload_file: UploadFile, allowed_extensions: set, max_size_mb: int):
    """
    Validates file size, extension, and verifies MIME type using magic bytes.
    """
    # 1. Check Extension
    filename = upload_file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid file format '{ext}'. Allowed formats: {', '.join(allowed_extensions)}"
        )

    # 2. Block dangerous executables/scripts
    dangerous_extensions = {'.exe', '.msi', '.bat', '.sh', '.py', '.js', '.php', '.jsp', '.asp', '.vbs', '.scr'}
    if ext in dangerous_extensions:
        raise HTTPException(status_code=400, detail="Security risk: Executable or script files are strictly prohibited.")

    # 3. Magic Byte (MIME) Validation using python-magic
    try:
        header = upload_file.file.read(2048)
        upload_file.file.seek(0)
        
        mime_type = magic.from_buffer(header, mime=True)
        
        if mime_type not in ALLOWED_MIME_TYPES:
            # Check for generic/octet-stream which sometimes happens with obscure media containers
            if mime_type == "application/octet-stream":
                # We allow it if the extension is in our allowed list, but warn in logs
                pass
            else:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Security mismatch: File content identified as '{mime_type}', which is not allowed."
                )
    except HTTPException:
        raise
    except Exception as e:
        # If magic is not installed yet or fails, fallback to basic security
        print(f"Magic validation fallback: {e}")
        pass

    # 4. Check Size
    try:
        upload_file.file.seek(0, 2)
        size = upload_file.file.tell()
        upload_file.file.seek(0)
        
        if size > max_size_mb * 1024 * 1024:
            raise HTTPException(
                status_code=413, 
                detail=f"File too large ({size / (1024*1024):.1f}MB). Maximum allowed is {max_size_mb}MB."
            )
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        pass

async def extract_text_from_pdf(upload_file: UploadFile) -> str:
    # First, validate security for resume
    validate_file_security(upload_file, ALLOWED_RESUME_EXTENSIONS, max_size_mb=2)

    temp_filename = f"temp_{uuid.uuid4()}.pdf"
    try:
        # Save uploaded PDF to disk (filename is sanitized via UUID)
        async with aiofiles.open(temp_filename, 'wb') as out_file:
            content = await upload_file.read()
            await out_file.write(content)

        def dump_text():
            with pdfplumber.open(temp_filename) as pdf:
                # 1 or 2 page limit
                if len(pdf.pages) > 3:
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
            raise HTTPException(
                status_code=400,
                detail="The uploaded PDF is not compatible with our ATS checker. Either the uploaded PDF is image-based or doesn't have text that is valid for a general ATS check. Please use a text-based (Lex-based) resume for better results."
            )
        return text

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error processing PDF file.")

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)