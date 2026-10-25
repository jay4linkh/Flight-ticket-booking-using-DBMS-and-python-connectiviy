"""SkyAgent: a Claude-powered travel agent that operates the reservation
database through typed tools and a sandboxed read-only SQL tool."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import anthropic

from . import services as svc
from .db import DEFAULT_DB, connect, schema_ddl

MODEL = os.environ.get("SKYAGENT_MODEL", "claude-opus-5-5")
MAX_TOOL_ROUNDS = 12


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "search_airports",
        "description": "Full-text search over airports by city, airport name, country or IATA code. "
                       "Use it to turn what the traveller typed (e.g. 'bombay', 'heathrow') into IATA codes.",
        "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "search_flights",
        "description": "Find bookable itineraries, cheapest first: direct flights plus one-stop connections "
                       "(60 min - 8 h layover). Fares are live dynamic prices that rise with load and "
                       "proximity to departure. Each itinerary lists its legs with flight_id, needed for booking.",
        "input_schema": _obj({
            "origin": {"type": "string", "description": "IATA code or city name"},
            "destination": {"type": "string", "description": "IATA code or city name"},
            "date": {"type": "string", "description": "Departure date, YYYY-MM-DD"},
            "cabin": {"type": "string", "enum": ["ECONOMY", "BUSINESS"]},
            "flex_days": {"type": "integer", "description": "Also search this many days either side (0-7)"},
            "include_connections": {"type": "boolean"},
        }, ["origin", "destination", "date"]),
    },
    {
        "name": "get_seat_map",
        "description": "Seat-by-seat availability for one flight with window/aisle flags, plus the current fare "
                       "in each cabin. Summarise it; never dump the whole map to the traveller.",
        "input_schema": _obj({"flight_id": {"type": "integer"}}, ["flight_id"]),
    },
    {
        "name": "hold_seat",
        "description": "Place a 15-minute hold on a seat at the current fare and return a PNR with status HELD. "
                       "Nothing is charged. Give either seat_no or let the system pick the best seat by "
                       "cabin and preference. For a connection, hold one seat per leg.",
        "input_schema": _obj({
            "flight_id": {"type": "integer"},
            "full_name": {"type": "string"},
            "email": {"type": "string"},
            "seat_no": {"type": "string", "description": "e.g. '12A'. Omit to auto-assign."},
            "cabin": {"type": "string", "enum": ["ECONOMY", "BUSINESS"]},
            "preference": {"type": "string", "enum": ["WINDOW", "AISLE", "ANY"]},
        }, ["flight_id", "full_name", "email"]),
    },
    {
        "name": "confirm_booking",
        "description": "Charge the traveller and turn a HELD booking into CONFIRMED. This takes money: only call "
                       "it after you have shown the flight, seat and exact fare and the traveller has explicitly "
                       "said yes in their latest message. Set traveller_confirmed accordingly.",
        "input_schema": _obj({
            "pnr": {"type": "string"},
            "payment_method": {"type": "string", "enum": ["CARD", "UPI", "WALLET"]},
            "traveller_confirmed": {"type": "boolean"},
        }, ["pnr", "payment_method", "traveller_confirmed"]),
    },
    {
        "name": "cancel_booking",
        "description": "Cancel a booking. Refund policy: 90% if 72h+ before departure, 50% if 6-72h, none "
                       "within 6h; holds are released free. Tell the traveller the refund before cancelling a "
                       "CONFIRMED booking and get their explicit yes.",
        "input_schema": _obj({"pnr": {"type": "string"}, "traveller_confirmed": {"type": "boolean"}},
                             ["pnr", "traveller_confirmed"]),
    },
    {
        "name": "change_seat",
        "description": "Move an active booking to another free seat in the same cabin.",
        "input_schema": _obj({"pnr": {"type": "string"}, "new_seat": {"type": "string"}}, ["pnr", "new_seat"]),
    },
    {
        "name": "get_booking",
        "description": "Look up one booking by PNR, or all bookings for an email address.",
        "input_schema": _obj({"pnr": {"type": "string"}, "email": {"type": "string"}}, []),
    },
    {
        "name": "query_database",
        "description": "Run one read-only SQL SELECT against the SQLite reservation database for analytical "
                       "questions the other tools do not cover (revenue, load factors, trends, audit history). "
                       "Writes are blocked at three levels. Results are capped at 200 rows; aggregate in SQL. "
                       "dynamic_fare(base_fare, load_factor, days_out, cabin) is available as a SQL function.",
        "input_schema": _obj({"sql": {"type": "string"}, "purpose": {"type": "string"}}, ["sql"]),
    },
]

SYSTEM_TEMPLATE = """You are SkyAgent, the booking agent for a multi-airline reservation system. You act on a live \
SQLite database through tools; tool results are the source of truth, so never invent flights, fares, seats or PNRs.

How to work:
- Resolve places to IATA codes, then search. If the date is unclear, ask, or search with flex_days.
- Present at most 3-4 good options in a compact list: flight numbers, times (UTC), stops, fare, seats left.
- Holding a seat is free and reversible, so you may hold once the traveller has picked an option and given name
  and email. Confirming charges money and cancelling a confirmed booking forfeits part of the fare: do either only
  after stating the exact amount and receiving an explicit yes in the traveller's latest message.
- For analytics questions, write efficient SQL with query_database and explain the result in plain words.
- Fares are in USD. Be concise and friendly.

Database schema:
{schema}"""


@dataclass
class AgentReply:
    text: str
    trace: list[dict] = field(default_factory=list)
    stop_reason: str | None = None


class SkyAgent:
    """One conversation. Holds the message history, so keep one per user session."""

    def __init__(self, db_path=DEFAULT_DB, client: anthropic.Anthropic | None = None):
        self.db_path = db_path
        self.conn = connect(db_path, actor="agent")
        self.client = client or anthropic.Anthropic()
        self.messages: list[dict] = []
        self.system = SYSTEM_TEMPLATE.format(schema=schema_ddl(self.conn))
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "search_airports": lambda a: svc.search_airports(self.conn, a["query"]),
            "search_flights": lambda a: svc.search_flights(
                self.conn, a["origin"], a["destination"], a["date"], a.get("cabin", "ECONOMY"),
                a.get("flex_days", 0), a.get("include_connections", True), limit=8),
            "get_seat_map": self._seat_map,
            "hold_seat": lambda a: svc.hold_seat(
                self.conn, a["flight_id"], a["full_name"], a["email"], a.get("seat_no"),
                a.get("cabin", "ECONOMY"), a.get("preference")),
            "confirm_booking": self._confirm,
            "cancel_booking": self._cancel,
            "change_seat": lambda a: svc.change_seat(self.conn, a["pnr"], a["new_seat"]),
            "get_booking": self._get_booking,
            "query_database": lambda a: svc.run_readonly_sql(self.db_path, a["sql"]),
        }

    # Tools with extra guards --------------------------------------------------

    def _seat_map(self, a):
        m = svc.seat_map(self.conn, a["flight_id"])
        free = [s for s in m["seats"] if s["available"]]
        # Compress: the model only needs free seats grouped by cabin, not 300 rows.
        by_cabin = {}
        for s in free:
            tag = "W" if s["is_window"] else "A" if s["is_aisle"] else "M"
            by_cabin.setdefault(s["cabin"], []).append(f"{s['seat_no']}{tag}")
        return {"flight": m["flight"], "fares": m["fares"], "free_seats": by_cabin,
                "legend": "suffix W=window A=aisle M=middle"}

    def _confirm(self, a):
        if not a.get("traveller_confirmed"):
            raise svc.BookingError("Ask the traveller to confirm the fare before charging.")
        return svc.confirm_booking(self.conn, a["pnr"], a["payment_method"])

    def _cancel(self, a):
        booking = svc.get_booking(self.conn, a["pnr"])
        if booking["status"] == "CONFIRMED" and not a.get("traveller_confirmed"):
            refund, policy = svc.refund_quote(booking["departs_at"], booking["fare"])
            raise svc.BookingError(f"Not cancelled. Tell the traveller: refund would be {refund} USD ({policy}), "
                                   "and ask them to confirm.")
        return svc.cancel_booking(self.conn, a["pnr"])

    def _get_booking(self, a):
        if a.get("pnr"):
            return svc.get_booking(self.conn, a["pnr"])
        if a.get("email"):
            return svc.passenger_bookings(self.conn, a["email"])
        raise svc.BookingError("Provide a pnr or an email.")

    # Loop ---------------------------------------------------------------------

    def run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        handler = self._handlers.get(name)
        if handler is None:
            return f"Unknown tool {name}", True
        try:
            return json.dumps(handler(args), default=str), False
        except svc.BookingError as e:
            return str(e), True
        except (KeyError, TypeError, ValueError) as e:
            return f"Invalid arguments for {name}: {e}", True

    def chat(self, user_text: str) -> AgentReply:
        today = datetime.now(timezone.utc).strftime("%A %Y-%m-%d %H:%M UTC")
        self.messages.append({"role": "user", "content": f"[now: {today}]\n{user_text}"})
        trace: list[dict] = []

        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=self.system,
                tools=TOOLS,
                messages=self.messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            # Keep the full content (thinking + tool_use blocks), not just text.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return AgentReply("Sorry, I can't help with that request.", trace, "refusal")
            if response.stop_reason != "tool_use":
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    text += "\n\n(Response was cut short.)"
                return AgentReply(text, trace, response.stop_reason)

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                output, is_error = self.run_tool(block.name, block.input)
                trace.append({"tool": block.name, "input": block.input, "error": is_error,
                              "output": output if len(output) < 1500 else output[:1500] + "..."})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})

        return AgentReply("I hit my step limit for this request. Could you narrow it down?", trace, "step_limit")
