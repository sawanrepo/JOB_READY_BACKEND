import logging
import time
import uuid
import os
from logging.handlers import TimedRotatingFileHandler
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from contextvars import ContextVar

# Context variable to store trace_id for the current request
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")

# Directories for logs
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

class TraceFormatter(logging.Formatter):
    def format(self, record):
        record.trace_id = trace_id_var.get()
        return super().format(record)

def setup_logging():
    # Base configuration
    log_format = "%(asctime)s | %(levelname)s | %(trace_id)s | %(name)s | %(message)s"
    formatter = TraceFormatter(log_format)

    # 1. Normal App Logger (10 days retention)
    app_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        when="D",
        interval=1,
        backupCount=10
    )
    app_handler.setFormatter(formatter)
    app_handler.setLevel(logging.INFO)

    # 2. Security & Error Logger (30 days retention)
    sec_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "security.log"),
        when="D",
        interval=1,
        backupCount=30
    )
    sec_handler.setFormatter(formatter)
    sec_handler.setLevel(logging.WARNING) # Only Warnings and Errors go here

    # Configure Root Logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(app_handler)
    root_logger.addHandler(sec_handler)
    
    # Also log to console for development
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = str(uuid.uuid4())
        trace_id_var.set(trace_id)
        
        start_time = time.time()
        
        # Determine user_id if possible (lightweight)
        user_id = "anonymous"
        auth_header = request.headers.get("Authorization")
        
        path = request.url.path
        method = request.method
        
        # Log Request Start (Metadata only)
        logging.info(f"REQ_START | {method} {path}")

        try:
            response = await call_next(request)
            duration = time.time() - start_time
            
            # Log Request End
            status_code = response.status_code
            log_msg = f"REQ_END | {method} {path} | STATUS: {status_code} | DUR: {duration:.3f}s"
            
            if status_code >= 400:
                logging.warning(log_msg)
            else:
                logging.info(log_msg)
            
            # Add trace_id to response headers for debugging
            response.headers["X-Trace-ID"] = trace_id
            return response
            
        except Exception as e:
            duration = time.time() - start_time
            logging.error(f"REQ_FAIL | {method} {path} | ERROR: {str(e)} | DUR: {duration:.3f}s", exc_info=True)
            raise e

# Specific Event Loggers
security_logger = logging.getLogger("security")
payment_logger = logging.getLogger("payment")

def log_security_event(event_type: str, user_id: str, metadata: dict = None):
    msg = f"SEC_EVENT | {event_type} | USER: {user_id} | DATA: {metadata or {}}"
    security_logger.warning(msg)

def log_payment_event(event_type: str, user_id: str, amount: float, status: str):
    msg = f"PAY_EVENT | {event_type} | USER: {user_id} | AMT: {amount} | STATUS: {status}"
    payment_logger.info(msg)
