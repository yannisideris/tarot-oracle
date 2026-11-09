"""Tarot LLM Oracle v3 - public web server.

One reading = one spread-planning call (JSON) + one streamed reading call,
both through the same Gemma 26B provider cascade as Aether Harbor.
Reading history lives in each visitor's browser and is sent with each request,
so visitors never see each other's readings.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import queue
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Deque, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import oracle as o
from .cascading_model import CascadingModelClient, ReadingCancelled

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tarot_oracle.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


READINGS_PER_HOUR = _env_int("TAROT_READINGS_PER_HOUR", 30)  # per visitor IP; 0 = unlimited
AUTO_LOOP_MAX = _env_int("TAROT_AUTO_LOOP_MAX", 10)  # readings per auto-loop run
MAX_JOBS = max(1, _env_int("TAROT_MAX_JOBS", 4))  # concurrent readings on the server
MAX_CARDS = max(1, _env_int("TAROT_MAX_CARDS", 12))
MAX_QUESTION_CHARS = 4000
MAX_HISTORY_RECORDS = 60

SETTINGS: Dict[str, Any] = {
    # Google first, so Tarot and Aether Harbor (Cloudflare first) start on different quotas.
    "active_provider_cascade": ["google", "cloudflare", "openrouter", "local"],
    "local": {"enabled": False},
}
CLIENT = CascadingModelClient(config=SETTINGS)
JOB_SLOTS = threading.BoundedSemaphore(MAX_JOBS)

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "spread_name": {"type": "string"},
        "rationale": {"type": "string"},
        "roles": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["spread_name", "rationale", "roles"],
    "additionalProperties": False,
}

app = FastAPI(title="Tarot LLM Oracle", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------- rate limit
_hits: Dict[str, Deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _take_rate_slot(ip: str) -> int:
    """Record a reading for this IP. Return 0 if allowed, else seconds to wait."""
    if READINGS_PER_HOUR <= 0:
        return 0
    now = time.time()
    with _hits_lock:
        hits = _hits[ip]
        while hits and now - hits[0] > 3600:
            hits.popleft()
        if len(hits) >= READINGS_PER_HOUR:
            return int(3600 - (now - hits[0])) + 1
        hits.append(now)
        if len(_hits) > 5000:  # forget idle visitors
            for key in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
                _hits.pop(key, None)
        return 0


# ------------------------------------------------------------------ models
class ReadingRequest(BaseModel):
    root_question: str = Field(..., max_length=MAX_QUESTION_CHARS)
    question: str = Field(..., max_length=MAX_QUESTION_CHARS)
    temperature: float = Field(0.85, ge=0.0, le=1.5)
    random_fallback: bool = True
    history: List[Dict[str, Any]] = Field(default_factory=list)


# ----------------------------------------------------------------- reading
def _plan(root_question: str, question: str, randomize_fallback: bool) -> o.SpreadPlan:
    messages = [
        {"role": "system", "content": o.SPREAD_PLANNER_PROMPT},
        {
            "role": "user",
            "content": (
                f"Root research anchor:\n{root_question}\n\n"
                f"Current question/facet:\n{question}\n\n"
                f"Use at most {MAX_CARDS} cards.\n"
                "Return the spread plan JSON now."
            ),
        },
    ]
    try:
        data = CLIENT.chat_json(messages, schema=PLAN_SCHEMA, temperature=0.25)
    except Exception as exc:  # planner failure is never fatal
        logger.warning("Spread planning failed: %s", exc)
        return o.fallback_spread_plan(randomize_fallback, "Fallback spread selected because planning failed.")
    plan = o.parse_spread_plan(json.dumps(data), randomize_fallback=randomize_fallback)
    plan.roles = plan.roles[:MAX_CARDS]
    return plan


def _run_reading(req: ReadingRequest, emit, cancelled: threading.Event) -> None:
    root_question = req.root_question.strip()
    question = req.question.strip()

    emit("status", {"text": "Waiting for a free oracle slot..."})
    while not JOB_SLOTS.acquire(timeout=0.5):
        if cancelled.is_set():
            return
    try:
        emit("status", {"text": "Planning spread with Gemma 26B..."})
        plan = _plan(root_question, question, req.random_fallback)
        if cancelled.is_set():
            return
        cards = o.draw_cards(len(plan.roles))
        history = [r for r in (o.record_from_dict(d) for d in req.history[-MAX_HISTORY_RECORDS:]) if r]
        relevant = o.select_relevant_history(history, root_question, question, cards)
        history_context = o.build_history_context(relevant)

        emit("plan", {
            "root_question": root_question,
            "question": question,
            "spread_name": plan.spread_name,
            "rationale": plan.rationale,
            "roles": plan.roles,
            "cards": [asdict(c) for c in cards],
        })
        emit("status", {"text": "Reading with Gemma 26B..."})

        messages = [
            {"role": "system", "content": o.SYSTEM_PROMPT},
            {"role": "user", "content": o.build_user_prompt(
                root_question, question, plan.spread_name, plan.roles, cards, history_context, plan.rationale,
            )},
        ]

        def on_token(text: str) -> None:
            if cancelled.is_set():
                raise ReadingCancelled()
            emit("token", {"text": text})

        def on_reset() -> None:
            if cancelled.is_set():
                raise ReadingCancelled()
            emit("reset", {})

        raw_final = CLIENT.stream_chat(messages, on_token=on_token, on_reset=on_reset, temperature=req.temperature)

        notes = o.extract_between(raw_final, "SYMBOLIC_NOTES_BEGIN", "SYMBOLIC_NOTES_END")
        reading_body = o.extract_between(raw_final, "READING_BEGIN", "READING_END") or raw_final
        record = o.ReadingRecord(
            timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            question=question,
            spread_name=plan.spread_name,
            roles=plan.roles,
            cards=[asdict(c) for c in cards],
            notes=notes,
            reading=reading_body,
            related_questions=o.parse_related_questions(reading_body),
            synthesis_update=o.parse_synthesis_update(reading_body),
            plan_rationale=plan.rationale,
            root_question=root_question,
            current_question=question,
        )
        emit("done", {"record": asdict(record)})
    except ReadingCancelled:
        return
    except Exception as exc:
        logger.warning("Reading failed: %s", exc)
        message = getattr(exc, "player_message", None) or "The oracle could not finish this reading. Please try again."
        emit("error", {"message": message})
    finally:
        JOB_SLOTS.release()


@app.post("/api/reading")
def reading(req: ReadingRequest, request: Request):
    if not req.root_question.strip() or not req.question.strip():
        raise HTTPException(400, "Write a question or theme first.")
    wait = _take_rate_slot(_client_ip(request))
    if wait:
        minutes = max(1, round(wait / 60))
        raise HTTPException(429, f"Reading limit reached ({READINGS_PER_HOUR} per hour). Try again in about {minutes} min.")

    events: "queue.Queue[tuple[str, dict] | None]" = queue.Queue()
    cancelled = threading.Event()

    def emit(kind: str, data: dict) -> None:
        events.put((kind, data))

    def worker() -> None:
        try:
            _run_reading(req, emit, cancelled)
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                try:
                    item = events.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            cancelled.set()  # client went away or stream ended

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------- misc
@app.get("/api/config")
def config():
    return {"auto_loop_max": AUTO_LOOP_MAX, "readings_per_hour": READINGS_PER_HOUR}


@app.get("/healthz")
def healthz():
    providers = {p: s.enabled for p, s in CLIENT.provider_statuses.items() if p != "local"}
    return JSONResponse({"ok": True, "providers": providers})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
