import logging
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base
from app.routers import auth, resume, payment, interview, audio_interview, subscription
from app.config import settings
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.utils.limiter import limiter
from fastapi.responses import JSONResponse
from fastapi import Request
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEMP_DIR = Path("temp_videos")


# Fix #20: clean up orphaned temp video files left over from crashes on startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    TEMP_DIR.mkdir(exist_ok=True)
    for f in TEMP_DIR.iterdir():
        try:
            f.unlink()
        except Exception:
            pass
    logger.info("Cleaned up temp_videos directory on startup")
    yield


app = FastAPI(
    title="Job Ready API",
    description="Backend for Job Ready application",
    version="1.0.0",
    lifespan=lifespan,
)

# Fix #18: restrict CORS to only the methods and headers the API actually uses
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Rate Limiting
app.state.limiter = limiter


def custom_rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"}
    )


app.add_exception_handler(RateLimitExceeded, custom_rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.mount("/resume/download", StaticFiles(directory="output"), name="resume-download")

# Include routers
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(resume.router, prefix="/resume", tags=["Resume"])
app.include_router(interview.router, prefix="/interview", tags=["Interview"])
app.include_router(audio_interview.router, prefix="/interview", tags=["Audio Interview"])
app.include_router(payment.router, prefix="/payment", tags=["Payment"])
app.include_router(subscription.router, prefix="/subscription", tags=["Subscription"])


@app.get("/")
def root():
    return {"message": "Job Ready API is running"}