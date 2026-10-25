"""FastAPI app: REST endpoints for the booking engine plus the agent chat."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import services as svc
from .agent import MODEL, SkyAgent
from .db import DEFAULT_DB, connect
from .seed import seed

STATIC = Path(__file__).with_name("static")

app = FastAPI(title="SkyAgent", description="Agentic flight reservation DBMS")
_sessions: dict[str, SkyAgent] = {}
_sessions_lock = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    seed(DEFAULT_DB)


def _db():
    return connect(DEFAULT_DB, actor="web")


def _call(fn, *args, **kwargs):
    conn = _db()
    try:
        return fn(conn, *args, **kwargs)
    except svc.BookingError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


class HoldIn(BaseModel):
    flight_id: int
    full_name: str
    email: str
    seat_no: str | None = None
    cabin: str = "ECONOMY"
    preference: str | None = None


class ConfirmIn(BaseModel):
    payment_method: str = "CARD"


class SqlIn(BaseModel):
    sql: str


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/airports")
def airports(q: str):
    return _call(svc.search_airports, q, 8)


@app.get("/api/flights")
def flights(origin: str, destination: str, date: str, cabin: str = "ECONOMY", flex_days: int = 0):
    return _call(svc.search_flights, origin, destination, date, cabin, flex_days)


@app.get("/api/flights/{flight_id}/seats")
def seats(flight_id: int):
    return _call(svc.seat_map, flight_id)


@app.post("/api/bookings/hold")
def hold(body: HoldIn):
    return _call(svc.hold_seat, body.flight_id, body.full_name, body.email, body.seat_no, body.cabin, body.preference)


@app.post("/api/bookings/{pnr}/confirm")
def confirm(pnr: str, body: ConfirmIn):
    return _call(svc.confirm_booking, pnr, body.payment_method)


@app.post("/api/bookings/{pnr}/cancel")
def cancel(pnr: str):
    return _call(svc.cancel_booking, pnr)


@app.get("/api/bookings/{pnr}")
def booking(pnr: str):
    return _call(svc.get_booking, pnr)


@app.get("/api/dashboard")
def dashboard():
    return _call(svc.dashboard)


@app.post("/api/sql")
def sql(body: SqlIn):
    try:
        return svc.run_readonly_sql(DEFAULT_DB, body.sql)
    except svc.BookingError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/agent")
def agent_chat(body: ChatIn):
    sid = body.session_id or uuid.uuid4().hex
    with _sessions_lock:
        agent = _sessions.get(sid)
        if agent is None:
            agent = _sessions[sid] = SkyAgent(DEFAULT_DB)
    try:
        reply = agent.chat(body.message)
    except anthropic.AuthenticationError as e:
        raise HTTPException(503, "The agent needs Claude API credentials: set ANTHROPIC_API_KEY and restart.") from e
    except anthropic.APIConnectionError as e:
        raise HTTPException(503, "Could not reach the Claude API.") from e
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error {e.status_code}: {e.message}") from e
    except TypeError as e:  # raised by the SDK when no credential source is configured
        if "api_key" in str(e) or "auth" in str(e).lower():
            raise HTTPException(503, "The agent needs Claude API credentials: set ANTHROPIC_API_KEY and restart.") from e
        raise
    return {"session_id": sid, "reply": reply.text, "trace": reply.trace, "model": MODEL}
