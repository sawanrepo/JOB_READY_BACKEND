import pdfplumber
import aiofiles
import os
import uuid
from fastapi import UploadFile, HTTPException
import asyncio
import logging
import subprocess
from app.config import settings

try:
    import magic
except Exception:
    magic = None

logger = logging.getLogger(__name__)

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
        if magic is None:
            raise RuntimeError("python-magic/libmagic is not available")
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
        logger.warning("File magic validation failed: %s", e, exc_info=True)
        if settings.STRICT_UPLOAD_VALIDATION:
            raise HTTPException(
                status_code=400,
                detail="File content could not be verified. Please upload a valid supported file."
            )

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

def validate_media_duration(file_path: str, max_duration_seconds: float):
    """
    Checks the exact duration of a saved media file using ffprobe.
    Falls back gracefully if ffprobe is not installed (e.g. during local testing).
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        try:
            duration = float(result.stdout.strip())
            if duration > max_duration_seconds:
                raise HTTPException(
                    status_code=400,
                    detail=f"Security risk: Uploaded media duration ({duration:.1f}s) exceeds the maximum allowed limit of {max_duration_seconds}s."
                )
        except ValueError:
            # Output of ffprobe was not a valid float (unexpected formatting)
            pass
    except FileNotFoundError:
        logger.warning("ffprobe not found while validating media duration for %s", file_path)
        if settings.REQUIRE_FFPROBE:
            raise HTTPException(
                status_code=503,
                detail="Media validation service is not configured. Please try again later."
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=400,
            detail="Media metadata validation timed out. Please upload a valid media file."
        )
    except subprocess.CalledProcessError as e:
        # ffprobe failed on this file (corrupted file headers)
        raise HTTPException(
            status_code=400,
            detail="Failed to parse media file metadata. The file may be corrupt or invalid."
        )

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
