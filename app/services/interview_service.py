import os
from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from app.config import settings
from app.schemas.interview import InterviewStartRequest, InterviewResponse, InterviewResult
from app.prompts import INTERVIEW_SYSTEM_PROMPT, INTERVIEW_ANALYSIS_PROMPT, INTERVIEW_REPORT_PROMPT, LIVE_VIDEO_INTERVIEW_ANSWER_PROMPT
from app.models.interview import InterviewSession
import json
import logging
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

GEMINI_MODELS = [
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
    'gemini-3-flash-preview',
    'gemini-3.1-flash-lite',
    'gemini-3.5-flash',
    'gemini-3.1-flash-lite-preview'
]

GEMINI_LIVE_MODELS = [
    'gemini-3.1-flash-live-preview'
]

VIDEO_TOTAL_QUESTIONS = 10
VIDEO_INTRO_QUESTION = (
    "Please introduce yourself briefly, including your background, key skills, "
    "and the kind of role you are preparing for."
)


def _is_retryable_ai_error(err: Exception) -> bool:
    err_str = str(err).lower()
    retryable_markers = (
        "500",
        "502",
        "503",
        "504",
        "internal",
        "deadline",
        "timeout",
        "temporarily",
        "overloaded",
        "unavailable",
        "high demand",
    )
    return any(marker in err_str for marker in retryable_markers)


def _raise_ai_http_exception(err: Exception, context: str) -> None:
    from fastapi import HTTPException

    err_str = str(err)
    err_lower = err_str.lower()
    if "429" in err_str or "quota" in err_lower:
        raise HTTPException(
            status_code=429,
            detail="AI service is temporarily rate-limited. Please wait a moment and try again."
        )
    if _is_retryable_ai_error(err):
        raise HTTPException(
            status_code=503,
            detail="AI service is temporarily unavailable. Please try again shortly."
        )
    logger.error("%s failed after all fallbacks: %s", context, err_str)
    raise HTTPException(status_code=500, detail="AI service failed. Please try again.")


def _strip_json_fence(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_video_analysis_response(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_json_fence(raw_text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return {
            "answer_text": "",
            "analysis": (raw_text or "").strip(),
            "behavior_observations": [],
        }

    if not isinstance(parsed, dict):
        return {
            "answer_text": "",
            "analysis": cleaned,
            "behavior_observations": [],
        }

    observations = parsed.get("behavior_observations") or []
    if isinstance(observations, str):
        observations = [observations]
    elif not isinstance(observations, list):
        observations = []

    return {
        "answer_text": (
            parsed.get("answer_text")
            or parsed.get("transcript")
            or parsed.get("candidate_answer")
            or ""
        ),
        "analysis": parsed.get("analysis") or parsed.get("feedback") or "",
        "behavior_observations": observations,
    }


def _parse_live_answer_response(raw_text: str) -> dict[str, Any]:
    parsed = _parse_video_analysis_response(raw_text)
    try:
        raw = json.loads(_strip_json_fence(raw_text))
    except Exception:
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    parsed["next_question"] = (raw.get("next_question") or "").strip()
    parsed["interview_ended"] = bool(raw.get("interview_ended"))
    return parsed


def _text_from_live_message(message: Any) -> str:
    text = getattr(message, "text", None)
    if text:
        return text

    server_content = getattr(message, "server_content", None)
    if not server_content:
        return ""

    output_transcription = getattr(server_content, "output_transcription", None)
    if output_transcription and getattr(output_transcription, "text", None):
        return output_transcription.text

    model_turn = getattr(server_content, "model_turn", None)
    if not model_turn:
        return ""

    chunks = []
    for part in getattr(model_turn, "parts", []) or []:
        part_text = getattr(part, "text", None)
        if part_text:
            chunks.append(part_text)
    return "".join(chunks)


def _format_interview_history(history: list[dict[str, Any]] | None) -> str:
    entries = []
    for item in history or []:
        if not item:
            continue
        question = item.get("question") or ""
        answer_text = item.get("answer_text") or ""
        analysis = item.get("analysis") or ""
        observations = item.get("behavior_observations") or []
        if isinstance(observations, str):
            observations = [observations]
        status = item.get("status") or "completed"
        parts = [f"Q: {question}"]
        if answer_text:
            parts.append(f"Candidate Answer: {answer_text}")
        if analysis:
            parts.append(f"Analysis: {analysis}")
        if observations:
            parts.append(f"Behavior Observations: {'; '.join(str(item) for item in observations)}")
        parts.append(f"Status: {status}")
        entries.append("\n".join(parts))
    return "\n\n".join(entries)


def _question_answer_pairs(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    return [
        {
            "question": item.get("question") or "",
            "answer_text": item.get("answer_text") or "",
            "status": item.get("status") or "completed",
        }
        for item in history or []
        if item
    ]


def _clean_question_text(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = _strip_json_fence(cleaned)
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    prefixes = ("Question:", "Next question:", "Q:")
    for prefix in prefixes:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned


def _question_already_asked(question: str, history: list[dict[str, Any]] | None) -> bool:
    normalized = " ".join((question or "").lower().split())
    if not normalized:
        return False
    for item in history or []:
        previous = " ".join((item.get("question") or "").lower().split())
        if previous == normalized:
            return True
    return False


class InterviewService:
    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.backup_client = (
            genai.Client(api_key=settings.GEMINI_API_KEY_BACKUP)
            if settings.GEMINI_API_KEY_BACKUP
            else None
        )
        self.model_id = 'gemini-2.5-flash'

    def _clients_with_labels(self):
        clients = [("primary", self.client)]
        if self.backup_client:
            clients.append(("backup", self.backup_client))
        return clients

    async def _generate_content_with_fallback(self, prompt: str, system_instruction: str = None, response_mime_type: str = None, temperature: float = 0.8) -> str:
        last_err = None
        for model in GEMINI_MODELS:
            for key_label, client in self._clients_with_labels():
                attempts = 2
                for attempt in range(1, attempts + 1):
                    try:
                        logger.info(
                            ">>> LLM CALL START | Model: %s | Key: %s | Prompt chars: %d | Attempt: %d",
                            model,
                            key_label,
                            len(prompt),
                            attempt,
                        )
                        config_args = {"temperature": temperature}
                        if system_instruction:
                            config_args["system_instruction"] = system_instruction
                        if response_mime_type:
                            config_args["response_mime_type"] = response_mime_type

                        response = await client.aio.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=types.GenerateContentConfig(**config_args)
                        )
                        if not response.text:
                            raise ValueError("Gemini returned an empty response")
                        logger.info("<<< LLM CALL SUCCESS | Model: %s | Key: %s | Attempt: %d", model, key_label, attempt)
                        return response.text
                    except Exception as e:
                        last_err = e
                        if attempt < attempts and _is_retryable_ai_error(e):
                            logger.warning(
                                "Retryable LLM failure for model %s using %s key on attempt %d: %s. Retrying...",
                                model,
                                key_label,
                                attempt,
                                e,
                            )
                            await asyncio.sleep(1.5 * attempt)
                            continue
                        logger.warning(
                            "LLM call failed for model %s using %s key on attempt %d: %s. Trying fallback...",
                            model,
                            key_label,
                            attempt,
                            e,
                        )
                        break

        _raise_ai_http_exception(last_err, "AI service")

    async def _generate_live_text_with_fallback(self, prompt: str, system_instruction: str | None = None, temperature: float = 0.8) -> str:
        last_err = None
        for model in GEMINI_LIVE_MODELS:
            for key_label, client in self._clients_with_labels():
                try:
                    logger.info(
                        ">>> GEMINI LIVE CALL START | Model: %s | Key: %s | Prompt chars: %d",
                        model,
                        key_label,
                        len(prompt),
                    )
                    config = types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        temperature=temperature,
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                        system_instruction=system_instruction,
                    )
                    async with client.aio.live.connect(model=model, config=config) as live_session:
                        user_turn = types.Content(
                            role="user",
                            parts=[types.Part(text=prompt)],
                        )
                        await live_session.send_client_content(turns=user_turn, turn_complete=True)
                        chunks = []
                        async for message in live_session.receive():
                            text = _text_from_live_message(message)
                            if text:
                                chunks.append(text)

                            server_content = getattr(message, "server_content", None)
                            if server_content and getattr(server_content, "turn_complete", False):
                                break

                        content = "".join(chunks).strip()
                        if not content:
                            raise ValueError("Gemini Live returned an empty response")
                        logger.info("<<< GEMINI LIVE CALL SUCCESS | Model: %s | Key: %s", model, key_label)
                        return content
                except Exception as e:
                    last_err = e
                    logger.warning("Gemini Live text call failed for model %s using %s key: %s. Trying fallback...", model, key_label, e)

        _raise_ai_http_exception(last_err, "Gemini Live service")

    async def start_interview(self, db: AsyncSession, user_id: int, request: InterviewStartRequest) -> InterviewResponse:
        # Check for any existing active session for this user
        existing_stmt = select(InterviewSession).where(
            InterviewSession.user_id == user_id,
            InterviewSession.interview_type == "video",
            InterviewSession.is_active == True
        )
        existing_result = await db.execute(existing_stmt)
        if existing_result.scalar_one_or_none():
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400, 
                detail="You already have an active video interview session. Please complete or resume it before starting a new one."
            )

        await db.execute(
            delete(InterviewSession).where(
                InterviewSession.user_id == user_id,
                InterviewSession.interview_type == "video",
                InterviewSession.is_active == False,
                InterviewSession.warnings_count >= 5,
            )
        )

        session_id = uuid.uuid4().hex

        
        # Initialize session in DB
        new_session = InterviewSession(
            id=session_id,
            user_id=user_id,
            interview_type="video",
            resume_text=request.resume_text,
            job_description=request.job_description,
            history=[],
            question_number=0,
            is_active=True,
            retakes={},
            processing_status="idle",
            result_status="pending",
        )
        db.add(new_session)
        await db.flush() # Get session_id into DB context
        
        # Generate first question
        logger.info(f"Starting persistent interview session {session_id} for user {user_id}")
        question = await self._generate_next_question(db, session_id)
        
        return InterviewResponse(
            session_id=session_id,
            question=question,
            question_number=new_session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            warnings_count=new_session.warnings_count or 0,
            is_active=new_session.is_active
        )

    async def process_response(self, db: AsyncSession, session_id: str, video_path: str, retaken: bool = False) -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Invalid or inactive session ID")

        # Retake grace is decided from server-side session state, never from the client flag.
        retakes = dict(session.retakes or {})
        retake_key = str(session.question_number)
        max_allowed = 300.0
        now = datetime.now(timezone.utc)
        served_at = session.question_started_at or session.updated_at
        if served_at.tzinfo is None:
            served_at = served_at.replace(tzinfo=timezone.utc)
        
        elapsed_seconds = (now - served_at).total_seconds()
        if elapsed_seconds > max_allowed:
            if retakes.get(retake_key):
                max_allowed = 300.0
            elif elapsed_seconds <= 360.0:
                max_allowed = 360.0
                retakes[retake_key] = True
                session.retakes = retakes

        if elapsed_seconds > max_allowed:
            current_q = session.current_question
            new_history = list(session.history or [])
            new_history.append({
                "question": current_q,
                "answer_text": (
                    "Candidate failed to answer this question within the time limit. "
                    f"Elapsed time: {elapsed_seconds:.0f} seconds."
                ),
                "analysis": (
                    "Candidate failed to answer this question within the time limit. "
                    f"Elapsed time: {elapsed_seconds:.0f} seconds."
                ),
                "status": "timed_out",
            })
            session.history = new_history

            try:
                next_q = await self._generate_next_question(db, session_id)
            except Exception:
                session.processing_status = "failed"
                await db.commit()
                raise

            ended = next_q == "INTERVIEW_END"
            if ended:
                session.is_active = False
                session.processing_status = "completed"
            else:
                session.processing_status = "idle"

            logger.info(
                "Video response timed out for session %s, question %s after %.0fs; advanced to question %s",
                session_id,
                retake_key,
                elapsed_seconds,
                session.question_number,
            )

            return InterviewResponse(
                session_id=session_id,
                question=next_q if not ended else "",
                question_number=session.question_number,
                total_questions=VIDEO_TOTAL_QUESTIONS,
                interview_ended=ended,
                warnings_count=session.warnings_count or 0,
                is_active=session.is_active,
            )
        
        current_q = session.current_question

        logger.info(f"Processing response for session {session_id}, question {session.question_number}")

        # 1. Analyze video response
        session.processing_status = "processing"
        try:
            analysis_data = await self._analyze_video(video_path, current_q)
        except Exception:
            session.processing_status = "failed"
            await db.commit()
            raise
        
        # 2. Update history in DB
        new_history = list(session.history or [])
        new_history.append({
            "question": current_q,
            "answer_text": analysis_data.get("answer_text") or "",
            "analysis": analysis_data.get("analysis") or "",
            "behavior_observations": analysis_data.get("behavior_observations") or [],
            "status": "completed",
        })
        session.history = new_history
        
        # 3. Generate next question
        next_q = await self._generate_next_question(db, session_id)
        
        ended = next_q == "INTERVIEW_END"
        
        if ended:
             session.is_active = False
             session.processing_status = "completed"
             logger.info(f"Interview ended for session {session_id}")
        else:
             session.processing_status = "idle"

        return InterviewResponse(
            session_id=session_id,
            question=next_q if not ended else "",
            question_number=session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            interview_ended=ended,
            warnings_count=session.warnings_count or 0,
            is_active=session.is_active
        )

    async def skip_response(self, db: AsyncSession, session_id: str, reason: str = "Candidate failed to answer this question within the time limit.") -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Invalid or inactive session ID")

        current_q = session.current_question
        logger.info("Skipping video response for session %s, question %s: %s", session_id, session.question_number, reason)

        new_history = list(session.history or [])
        new_history.append({
            "question": current_q,
            "answer_text": reason,
            "analysis": reason,
            "status": "skipped",
        })
        session.history = new_history

        next_q = await self._generate_next_question(db, session_id)
        ended = next_q == "INTERVIEW_END"

        if ended:
            session.is_active = False
            session.processing_status = "completed"
        else:
            session.processing_status = "idle"

        return InterviewResponse(
            session_id=session_id,
            question=next_q if not ended else "",
            question_number=session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            interview_ended=ended,
            warnings_count=session.warnings_count or 0,
            is_active=session.is_active
        )

    async def grant_resume_grace(self, db: AsyncSession, session_id: str) -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Invalid or inactive session ID")

        retakes = dict(session.retakes or {})
        grace_key = "video_resume_grace_used"
        if not retakes.get(grace_key):
            retakes[grace_key] = {
                "question_number": session.question_number,
                "granted_at": datetime.now(timezone.utc).isoformat(),
            }
            session.retakes = retakes
            session.question_started_at = datetime.now(timezone.utc)
            logger.info("Granted one-time video resume grace for session %s, question %s", session_id, session.question_number)

        return InterviewResponse(
            session_id=session_id,
            question=session.current_question or "Starting interview...",
            question_number=session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            interview_ended=not session.is_active,
            warnings_count=session.warnings_count or 0,
            is_active=session.is_active
        )

    async def _generate_next_question(self, db: AsyncSession, session_id: str) -> str:
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        session.question_number += 1
        
        if session.question_number > VIDEO_TOTAL_QUESTIONS:
            return "INTERVIEW_END"

        if session.question_number == 1:
            session.current_question = VIDEO_INTRO_QUESTION
            session.question_started_at = datetime.now(timezone.utc)
            return session.current_question

        # Construct prompt
        history_text = _format_interview_history(session.history)
        interview_seed = f"{session.id}:{session.question_number}:{len(session.history or [])}:{datetime.now(timezone.utc).isoformat()}"
        prompt = INTERVIEW_SYSTEM_PROMPT.format(
            question_number=session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            last_question=session.current_question if session.current_question else "None (Start of Interview)",
            interview_seed=interview_seed,
            resume_summary=session.resume_text[:800] + "..." if len(session.resume_text) > 800 else session.resume_text,
            jd_summary=session.job_description[:800] + "..." if len(session.job_description) > 800 else session.job_description,
            history=history_text if history_text else "None"
        )
        
        try:
            next_q = await self._generate_live_text_with_fallback(
                prompt=prompt,
                temperature=0.95
            )
        except Exception as e:
            logger.error("!!! GEMINI LIVE CALL FAILED [_generate_next_question]: %s", e, exc_info=True)
            raise
            
        session.current_question = _clean_question_text(next_q)
        session.question_started_at = datetime.now(timezone.utc)
        return session.current_question

    async def process_live_response_stream(self, db: AsyncSession, session_id: str, websocket: Any) -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True,
            InterviewSession.interview_type == "video",
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Invalid or inactive session ID")

        if not session.current_question:
            await self._generate_next_question(db, session_id)
            await db.flush()
            result = await db.execute(stmt)
            session = result.scalar_one_or_none()

        prompt = self._build_live_answer_prompt(session)
        last_err = None

        for model in GEMINI_LIVE_MODELS:
            for key_label, client in self._clients_with_labels():
                media_started = False
                try:
                    logger.info(">>> GEMINI LIVE STREAM START | Model: %s | Key: %s | Session: %s", model, key_label, session_id)
                    config = types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        temperature=0.35,
                        system_instruction=prompt,
                        input_audio_transcription=types.AudioTranscriptionConfig(),
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                        realtime_input_config=types.RealtimeInputConfig(
                            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
                        ),
                    )

                    async with client.aio.live.connect(model=model, config=config) as live_session:
                        await live_session.send_realtime_input(activity_start=types.ActivityStart())
                        media_started = True
                        await websocket.send_json({
                            "type": "listening",
                            "message": "Live model is listening to your answer.",
                        })

                        while True:
                            message = await websocket.receive()
                            if message.get("type") == "websocket.disconnect":
                                raise RuntimeError("Client disconnected during live interview")

                            audio_bytes = message.get("bytes")
                            if audio_bytes:
                                await live_session.send_realtime_input(
                                    audio=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
                                )
                                continue

                            text_payload = message.get("text")
                            if not text_payload:
                                continue

                            payload = json.loads(text_payload)
                            payload_type = payload.get("type")
                            if payload_type == "video_frame":
                                frame_data = payload.get("data") or ""
                                if frame_data:
                                    import base64
                                    await live_session.send_realtime_input(
                                        video=types.Blob(
                                            data=base64.b64decode(frame_data),
                                            mime_type=payload.get("mime_type") or "image/jpeg",
                                        )
                                    )
                            elif payload_type == "finish_answer":
                                break
                            elif payload_type == "cancel":
                                raise RuntimeError("Live interview answer cancelled by client")

                        await live_session.send_realtime_input(activity_end=types.ActivityEnd())
                        await websocket.send_json({
                            "type": "processing",
                            "message": "Generating next question from your live answer...",
                        })

                        live_response = await self._collect_live_answer_response(live_session)
                        finalized = await self._finalize_live_answer(db, session_id, live_response)
                        logger.info("<<< GEMINI LIVE STREAM SUCCESS | Model: %s | Key: %s | Session: %s", model, key_label, session_id)
                        return finalized
                except Exception as e:
                    last_err = e
                    if media_started:
                        logger.error("Gemini Live stream failed after media started: %s", e, exc_info=True)
                        raise
                    logger.warning("Gemini Live stream could not start for model %s using %s key: %s. Trying fallback...", model, key_label, e)

        _raise_ai_http_exception(last_err, "Gemini Live stream")

    def _build_live_answer_prompt(self, session: InterviewSession) -> str:
        history_text = _format_interview_history(session.history)
        interview_seed = f"{session.id}:{session.question_number}:{len(session.history or [])}:{datetime.now(timezone.utc).isoformat()}"
        return LIVE_VIDEO_INTERVIEW_ANSWER_PROMPT.format(
            question_number=session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            current_question=session.current_question or "",
            interview_seed=interview_seed,
            resume_summary=session.resume_text[:800] + "..." if len(session.resume_text) > 800 else session.resume_text,
            jd_summary=session.job_description[:800] + "..." if len(session.job_description) > 800 else session.job_description,
            history=history_text if history_text else "None",
        )

    async def _collect_live_answer_response(self, live_session: Any) -> dict[str, Any]:
        chunks = []
        input_transcript_chunks = []
        receiver = live_session.receive().__aiter__()

        while True:
            try:
                message = await asyncio.wait_for(receiver.__anext__(), timeout=90)
            except StopAsyncIteration:
                break

            text = _text_from_live_message(message)
            if text:
                chunks.append(text)

            server_content = getattr(message, "server_content", None)
            if server_content:
                input_transcription = getattr(server_content, "input_transcription", None)
                if input_transcription and getattr(input_transcription, "text", None):
                    input_transcript_chunks.append(input_transcription.text)

                if getattr(server_content, "turn_complete", False):
                    break

        raw_text = "".join(chunks).strip()
        if not raw_text:
            raise ValueError("Gemini Live returned an empty answer analysis")

        parsed = _parse_live_answer_response(raw_text)
        if not parsed.get("answer_text") and input_transcript_chunks:
            parsed["answer_text"] = " ".join(input_transcript_chunks).strip()
        return parsed

    async def _finalize_live_answer(self, db: AsyncSession, session_id: str, analysis_data: dict[str, Any]) -> InterviewResponse:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id,
            InterviewSession.is_active == True,
            InterviewSession.interview_type == "video",
        ).with_for_update()
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise ValueError("Invalid or inactive session ID")

        current_q = session.current_question
        new_history = list(session.history or [])
        new_history.append({
            "question": current_q,
            "answer_text": analysis_data.get("answer_text") or "",
            "analysis": analysis_data.get("analysis") or "",
            "behavior_observations": analysis_data.get("behavior_observations") or [],
            "status": "completed",
            "source": "gemini_live",
        })
        session.history = new_history

        # The backend owns interview length. Gemini Live may occasionally mark
        # interview_ended early, but results require all answers.
        should_end = session.question_number >= VIDEO_TOTAL_QUESTIONS
        if should_end:
            session.is_active = False
            session.processing_status = "completed"
            return InterviewResponse(
                session_id=session_id,
                question="",
                question_number=session.question_number,
                total_questions=VIDEO_TOTAL_QUESTIONS,
                interview_ended=True,
                warnings_count=session.warnings_count or 0,
                is_active=False,
            )

        next_question = _clean_question_text(analysis_data.get("next_question") or "")
        if _question_already_asked(next_question, session.history):
            logger.warning("Gemini Live returned a repeated question for session %s; regenerating.", session_id)
            next_question = ""
        if not next_question:
            next_question = await self._generate_next_question(db, session_id)
        else:
            session.question_number += 1
            session.current_question = next_question
            session.question_started_at = datetime.now(timezone.utc)

        session.processing_status = "idle"
        return InterviewResponse(
            session_id=session_id,
            question=next_question,
            question_number=session.question_number,
            total_questions=VIDEO_TOTAL_QUESTIONS,
            interview_ended=False,
            warnings_count=session.warnings_count or 0,
            is_active=True,
        )

    async def _upload_video_for_analysis(self, client, key_label: str, video_path: str):
        logger.info("Uploading video %s to Gemini using %s key...", video_path, key_label)
        video_file = await client.aio.files.upload(file=video_path)

        deadline = asyncio.get_running_loop().time() + settings.GEMINI_FILE_PROCESSING_TIMEOUT_SECONDS
        while True:
            file_info = await client.aio.files.get(name=video_file.name)
            if file_info.state.name != 'PROCESSING':
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Video processing timed out")
            await asyncio.sleep(2)

        if file_info.state.name == 'FAILED':
            raise ValueError("Video processing failed by Gemini")

        return video_file

    async def _analyze_video(self, video_path: str, question: str) -> dict[str, Any]:
        uploaded_files = {}

        prompt = INTERVIEW_ANALYSIS_PROMPT.format(question=question)
        
        last_err = None
        for model in GEMINI_MODELS:
            for key_label, client in self._clients_with_labels():
                try:
                    if key_label not in uploaded_files:
                        uploaded_files[key_label] = await self._upload_video_for_analysis(client, key_label, video_path)
                    video_file = uploaded_files[key_label]
                except Exception as e:
                    last_err = e
                    logger.warning("Video upload failed using %s key for model %s: %s. Trying fallback...", key_label, model, e)
                    continue

                attempts = 2
                for attempt in range(1, attempts + 1):
                    try:
                        logger.info(
                            ">>> LLM CALL START [_analyze_video] | Model: %s | Key: %s | Attempt: %d",
                            model,
                            key_label,
                            attempt,
                        )
                        response = await client.aio.models.generate_content(
                            model=model,
                            contents=[prompt, video_file],
                            config=types.GenerateContentConfig(
                                temperature=0.2,
                                response_mime_type="application/json",
                            )
                        )
                        if not response.text:
                            raise ValueError("Gemini returned an empty video analysis")
                        logger.info("<<< LLM CALL SUCCESS [_analyze_video] | Model: %s | Key: %s | Attempt: %d", model, key_label, attempt)
                        return _parse_video_analysis_response(response.text)
                    except Exception as e:
                        last_err = e
                        if attempt < attempts and _is_retryable_ai_error(e):
                            logger.warning(
                                "Retryable video analysis failure for model %s using %s key on attempt %d: %s. Retrying...",
                                model,
                                key_label,
                                attempt,
                                e
                            )
                            await asyncio.sleep(1.5 * attempt)
                            continue
                        logger.warning(
                            "LLM call failed for model %s using %s key during video analysis on attempt %d: %s. Trying fallback...",
                            model,
                            key_label,
                            attempt,
                            e,
                        )
                        break

        _raise_ai_http_exception(last_err, "AI video analysis")

    async def generate_result(self, db: AsyncSession, session_id: str) -> InterviewResult:
        stmt = select(InterviewSession).where(InterviewSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            raise ValueError("Invalid session ID")

        # DISCARD logic: No result if warnings reach 5
        if (session.warnings_count or 0) >= 5:
            session.is_active = False
            await db.commit()
            raise ValueError("Interview discarded due to malpractice (too many warnings). No result generated.")

        history_text = _format_interview_history(session.history)
        question_answer_pairs = _question_answer_pairs(session.history)
        
        prompt = INTERVIEW_REPORT_PROMPT.format(
            resume_text=session.resume_text,
            job_description=session.job_description,
            history=history_text
        )
        
        try:
            content_text = await self._generate_content_with_fallback(
                prompt=prompt,
                response_mime_type="application/json"
            )
            session.is_active = False # Deactivate after completion
            raw_result = json.loads(content_text)
            
            # Manual Sanitization to prevent ResponseValidationError
            sanitized = {
                "communication_score": raw_result.get("communication_score") or raw_result.get("communication", 0),
                "technical_knowledge_score": raw_result.get("technical_knowledge_score") or raw_result.get("technical_knowledge", 0),
                "problem_solving_score": raw_result.get("problem_solving_score") or raw_result.get("problem_solving", 0),
                "confidence_score": raw_result.get("confidence_score") or raw_result.get("confidence", 0),
                "strengths": raw_result.get("strengths") or [],
                "weaknesses": raw_result.get("weaknesses") or [],
                "final_verdict": raw_result.get("final_verdict") or "Needs Improvement",
                "feedback_summary": raw_result.get("feedback_summary") or ""
            }
            
            # Ensure improvement_suggestions is a list
            suggestions = raw_result.get("improvement_suggestions") or []
            if isinstance(suggestions, str):
                sanitized["improvement_suggestions"] = [suggestions]
            else:
                sanitized["improvement_suggestions"] = suggestions

            sanitized["question_answer_pairs"] = question_answer_pairs
                
            return sanitized
        except Exception as e:
            logger.error("!!! LLM CALL FAILED [generate_result]: %s", e, exc_info=True)
            from fastapi import HTTPException
            if isinstance(e, HTTPException):
                raise e
            raise ValueError("Failed to generate valid report JSON")
