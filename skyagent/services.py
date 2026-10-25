"""Booking domain logic. Every function takes a connection so the web API, the
CLI and the agent's tools share one implementation."""

from __future__ import annotations

import random
import re
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone

from .db import ISO, connect, transaction

HOLD_MINUTES = 15
CABINS = ("ECONOMY", "BUSINESS")
PAYMENT_METHODS = ("CARD", "UPI", "WALLET")


class BookingError(Exception):
    """A rule violation the caller (or the agent) should explain to the user."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def expire_holds(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE bookings SET status = 'EXPIRED' WHERE status = 'HELD' AND hold_expires_at < ?",
        (_now().strftime(ISO),),
    )
    return cur.rowcount


# --- Airports ---------------------------------------------------------------

def search_airports(conn, query: str, limit: int = 5) -> list[dict]:
    tokens = re.findall(r"[A-Za-z0-9]+", query or "")
    if not tokens:
        return []
    match = " ".join(f'"{t}"*' for t in tokens)
    return _rows(conn.execute(
        "SELECT a.iata, a.name, a.city, a.country FROM airports_fts f "
        "JOIN airports a ON a.rowid = f.rowid WHERE airports_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    ))


def resolve_airport(conn, text: str) -> str:
    code = (text or "").strip().upper()
    if len(code) == 3 and conn.execute("SELECT 1 FROM airports WHERE iata = ?", (code,)).fetchone():
        return code
    hits = search_airports(conn, text, 1)
    if not hits:
        raise BookingError(f"Unknown airport or city: {text!r}")
    return hits[0]["iata"]


# --- Search -----------------------------------------------------------------

# Shared fragment: live price and availability for one flight in one cabin.
_LEG = """
    SELECT f.id AS flight_id, f.flight_no, al.name AS airline, r.origin, r.destination,
           f.departs_at, f.arrives_at, f.status, ac.model AS aircraft, r.distance_km,
           (SELECT count(*) FROM seats s WHERE s.flight_id = f.id AND s.cabin = :cabin)
             - (SELECT count(*) FROM bookings b JOIN seats s
                  ON s.flight_id = b.flight_id AND s.seat_no = b.seat_no
                 WHERE b.flight_id = f.id AND s.cabin = :cabin
                   AND b.status IN ('HELD', 'CONFIRMED')) AS seats_left,
           dynamic_fare(
               r.base_fare,
               (SELECT count(*) FROM bookings b WHERE b.flight_id = f.id AND b.status IN ('HELD','CONFIRMED'))
                 * 1.0 / (SELECT count(*) FROM seats s WHERE s.flight_id = f.id),
               julianday(f.departs_at) - julianday('now'),
               :cabin) AS fare
    FROM flights f
    JOIN routes r ON r.id = f.route_id
    JOIN airlines al ON al.code = r.airline
    JOIN aircraft ac ON ac.id = f.aircraft_id
    WHERE f.status IN ('SCHEDULED', 'DELAYED') AND f.departs_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
"""


def search_flights(conn, origin: str, destination: str, date: str, cabin: str = "ECONOMY",
                   flex_days: int = 0, include_connections: bool = True, limit: int = 20) -> list[dict]:
    """Direct flights plus one-stop itineraries (60 min to 8 h layover),
    cheapest first. date is YYYY-MM-DD; flex_days widens the window both ways."""
    cabin = cabin.upper()
    if cabin not in CABINS:
        raise BookingError(f"cabin must be one of {CABINS}")
    o, d = resolve_airport(conn, origin), resolve_airport(conn, destination)
    if o == d:
        raise BookingError("Origin and destination are the same airport.")
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise BookingError("date must be YYYY-MM-DD") from e
    flex_days = max(0, min(int(flex_days), 7))
    lo = (day - timedelta(days=flex_days)).strftime(ISO)
    hi = (day + timedelta(days=flex_days + 1)).strftime(ISO)
    params = {"cabin": cabin, "o": o, "d": d, "lo": lo, "hi": hi, "limit": limit}

    direct = _rows(conn.execute(
        f"SELECT * FROM ({_LEG}) WHERE origin = :o AND destination = :d "
        "AND departs_at >= :lo AND departs_at < :hi AND seats_left > 0 ORDER BY fare LIMIT :limit",
        params,
    ))
    itineraries = [{"type": "DIRECT", "stops": 0, "total_fare": round(x["fare"], 2),
                    "departs_at": x["departs_at"], "arrives_at": x["arrives_at"], "legs": [x]} for x in direct]

    if include_connections:
        conns = _rows(conn.execute(
            f"""
            WITH leg AS ({_LEG})
            SELECT a.flight_id AS a_id, b.flight_id AS b_id, a.fare + b.fare AS total_fare,
                   (julianday(b.departs_at) - julianday(a.arrives_at)) * 24 * 60 AS layover_min
            FROM leg a JOIN leg b ON b.origin = a.destination
            WHERE a.origin = :o AND b.destination = :d
              AND a.departs_at >= :lo AND a.departs_at < :hi
              AND b.departs_at >= strftime('%Y-%m-%dT%H:%M:%SZ', a.arrives_at, '+60 minutes')
              AND b.departs_at <= strftime('%Y-%m-%dT%H:%M:%SZ', a.arrives_at, '+8 hours')
              AND a.seats_left > 0 AND b.seats_left > 0
            ORDER BY total_fare LIMIT :limit
            """,
            params,
        ))
        ids = sorted({c["a_id"] for c in conns} | {c["b_id"] for c in conns})
        legs_by_id = {}
        if ids:
            # ids are integers read back from the database, safe to inline.
            legs_by_id = {r["flight_id"]: r for r in _rows(conn.execute(
                f"SELECT * FROM ({_LEG}) WHERE flight_id IN ({','.join(map(str, map(int, ids)))})",
                {"cabin": cabin},
            ))}
        for c in conns:
            a, b = legs_by_id[c["a_id"]], legs_by_id[c["b_id"]]
            itineraries.append({"type": "CONNECTION", "stops": 1, "via": a["destination"],
                                "layover_minutes": int(round(c["layover_min"])),
                                "total_fare": round(c["total_fare"], 2),
                                "departs_at": a["departs_at"], "arrives_at": b["arrives_at"], "legs": [a, b]})

    itineraries.sort(key=lambda i: (i["total_fare"], i["stops"]))
    return itineraries[:limit]


# --- Seats ------------------------------------------------------------------

def seat_map(conn, flight_id: int) -> dict:
    expire_holds(conn)
    flight = conn.execute("SELECT * FROM v_flight_board WHERE flight_id = ?", (flight_id,)).fetchone()
    if not flight:
        raise BookingError(f"No flight with id {flight_id}")
    seats = _rows(conn.execute(
        """
        SELECT s.seat_no, s.cabin, s.is_window, s.is_aisle,
               CAST(substr(s.seat_no, 1, length(s.seat_no) - 1) AS INTEGER) AS row_no,
               substr(s.seat_no, -1) AS letter,
               b.pnr IS NULL AS available
        FROM seats s
        LEFT JOIN bookings b ON b.flight_id = s.flight_id AND b.seat_no = s.seat_no
                            AND b.status IN ('HELD', 'CONFIRMED')
        WHERE s.flight_id = ?
        ORDER BY row_no, letter
        """,
        (flight_id,),
    ))
    fares = {c: _quote(conn, flight_id, c) for c in CABINS}
    return {"flight": dict(flight), "fares": fares, "seats": seats}


def _quote(conn, flight_id: int, cabin: str) -> float:
    row = conn.execute(f"SELECT fare FROM ({_LEG}) WHERE flight_id = :f", {"cabin": cabin, "f": flight_id}).fetchone()
    if not row:
        raise BookingError("Flight is not open for booking.")
    return round(row["fare"], 2)


def _pick_seat(conn, flight_id: int, cabin: str, preference: str | None) -> str:
    order = {"WINDOW": "s.is_window DESC,", "AISLE": "s.is_aisle DESC,"}.get((preference or "").upper(), "")
    row = conn.execute(
        f"""
        SELECT s.seat_no FROM seats s
        WHERE s.flight_id = ? AND s.cabin = ?
          AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.flight_id = s.flight_id
                          AND b.seat_no = s.seat_no AND b.status IN ('HELD', 'CONFIRMED'))
        ORDER BY {order} CAST(substr(s.seat_no, 1, length(s.seat_no) - 1) AS INTEGER), s.seat_no
        LIMIT 1
        """,
        (flight_id, cabin),
    ).fetchone()
    if not row:
        raise BookingError(f"No {cabin.lower()} seats left on this flight.")
    return row["seat_no"]


# --- Booking lifecycle --------------------------------------------------------

def _new_pnr(conn) -> str:
    while True:
        pnr = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not conn.execute("SELECT 1 FROM bookings WHERE pnr = ?", (pnr,)).fetchone():
            return pnr


def hold_seat(conn, flight_id: int, full_name: str, email: str, seat_no: str | None = None,
              cabin: str = "ECONOMY", preference: str | None = None) -> dict:
    """Reserves a seat for HOLD_MINUTES at the current dynamic fare."""
    cabin = cabin.upper()
    with transaction(conn):
        expire_holds(conn)
        if seat_no:
            seat = conn.execute("SELECT cabin FROM seats WHERE flight_id = ? AND seat_no = ?",
                                (flight_id, seat_no.upper())).fetchone()
            if not seat:
                raise BookingError(f"Seat {seat_no} does not exist on flight {flight_id}.")
            seat_no, cabin = seat_no.upper(), seat["cabin"]
        else:
            seat_no = _pick_seat(conn, flight_id, cabin, preference)
        fare = _quote(conn, flight_id, cabin)
        try:
            pid = conn.execute(
                "INSERT INTO passengers(full_name, email) VALUES (?, ?) "
                "ON CONFLICT(email) DO UPDATE SET full_name = excluded.full_name RETURNING id",
                (full_name.strip(), email.strip()),
            ).fetchone()["id"]
            pnr = _new_pnr(conn)
            expires = (_now() + timedelta(minutes=HOLD_MINUTES)).strftime(ISO)
            conn.execute(
                "INSERT INTO bookings(pnr, passenger_id, flight_id, seat_no, fare, status, hold_expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'HELD', ?)",
                (pnr, pid, flight_id, seat_no, fare, expires),
            )
        except sqlite3.IntegrityError as e:
            msg = str(e)
            if "bookings.flight_id, bookings.seat_no" in msg:
                raise BookingError(f"Seat {seat_no} was just taken. Pick another seat.") from e
            if "CHECK constraint" in msg:
                raise BookingError("Passenger name or email looks invalid.") from e
            raise BookingError(msg) from e
        except sqlite3.DatabaseError as e:  # RAISE(ABORT) from triggers
            raise BookingError(str(e)) from e
    return get_booking(conn, pnr)


def confirm_booking(conn, pnr: str, payment_method: str = "CARD") -> dict:
    method = payment_method.upper()
    if method not in PAYMENT_METHODS:
        raise BookingError(f"payment_method must be one of {PAYMENT_METHODS}")
    with transaction(conn):
        expire_holds(conn)
        b = conn.execute("SELECT status, fare FROM bookings WHERE pnr = ?", (pnr.upper(),)).fetchone()
        if not b:
            raise BookingError(f"No booking {pnr}")
        if b["status"] != "HELD":
            raise BookingError(f"Booking {pnr} is {b['status']}; only HELD bookings can be confirmed.")
        conn.execute("UPDATE bookings SET status = 'CONFIRMED', hold_expires_at = NULL WHERE pnr = ?", (pnr.upper(),))
        conn.execute("INSERT INTO payments(pnr, amount, method) VALUES (?, ?, ?)", (pnr.upper(), b["fare"], method))
    return get_booking(conn, pnr)


def refund_quote(departs_at: str, fare: float) -> tuple[float, str]:
    hours = (datetime.strptime(departs_at, ISO).replace(tzinfo=timezone.utc) - _now()).total_seconds() / 3600
    if hours >= 72:
        return round(fare * 0.9, 2), "90% refund (cancelled 72h+ before departure)"
    if hours >= 6:
        return round(fare * 0.5, 2), "50% refund (cancelled 6-72h before departure)"
    return 0.0, "no refund (cancelled within 6h of departure)"


def cancel_booking(conn, pnr: str) -> dict:
    pnr = pnr.upper()
    with transaction(conn):
        b = conn.execute(
            "SELECT b.status, b.fare, f.departs_at FROM bookings b JOIN flights f ON f.id = b.flight_id WHERE pnr = ?",
            (pnr,),
        ).fetchone()
        if not b:
            raise BookingError(f"No booking {pnr}")
        if b["status"] not in ("HELD", "CONFIRMED"):
            raise BookingError(f"Booking {pnr} is already {b['status']}.")
        refund, policy = (0.0, "hold released, nothing was charged") if b["status"] == "HELD" \
            else refund_quote(b["departs_at"], b["fare"])
        conn.execute("UPDATE bookings SET status = 'CANCELLED' WHERE pnr = ?", (pnr,))
        if refund:
            conn.execute("INSERT INTO payments(pnr, amount, method) VALUES (?, ?, 'REFUND')", (pnr, -refund))
    out = get_booking(conn, pnr)
    out.update(refund=refund, refund_policy=policy)
    return out


def change_seat(conn, pnr: str, new_seat: str) -> dict:
    pnr, new_seat = pnr.upper(), new_seat.upper()
    with transaction(conn):
        b = conn.execute("SELECT flight_id, seat_no, status FROM bookings WHERE pnr = ?", (pnr,)).fetchone()
        if not b or b["status"] not in ("HELD", "CONFIRMED"):
            raise BookingError(f"No active booking {pnr}")
        old = conn.execute("SELECT cabin FROM seats WHERE flight_id = ? AND seat_no = ?",
                           (b["flight_id"], b["seat_no"])).fetchone()
        new = conn.execute("SELECT cabin FROM seats WHERE flight_id = ? AND seat_no = ?",
                           (b["flight_id"], new_seat)).fetchone()
        if not new:
            raise BookingError(f"Seat {new_seat} does not exist.")
        if new["cabin"] != old["cabin"]:
            raise BookingError("Seat changes must stay in the same cabin.")
        try:
            conn.execute("UPDATE bookings SET seat_no = ? WHERE pnr = ?", (new_seat, pnr))
        except sqlite3.IntegrityError as e:
            raise BookingError(f"Seat {new_seat} is taken.") from e
    return get_booking(conn, pnr)


def get_booking(conn, pnr: str) -> dict:
    row = conn.execute(
        """
        SELECT b.pnr, b.status, b.seat_no, s.cabin, b.fare, b.hold_expires_at, b.created_at,
               p.full_name, p.email, p.loyalty_points,
               fb.flight_no, fb.airline, fb.origin, fb.destination, fb.departs_at, fb.arrives_at,
               fb.status AS flight_status, fb.flight_id
        FROM bookings b
        JOIN passengers p ON p.id = b.passenger_id
        JOIN seats s ON s.flight_id = b.flight_id AND s.seat_no = b.seat_no
        JOIN v_flight_board fb ON fb.flight_id = b.flight_id
        WHERE b.pnr = ?
        """,
        (pnr.upper(),),
    ).fetchone()
    if not row:
        raise BookingError(f"No booking {pnr}")
    return dict(row)


def passenger_bookings(conn, email: str) -> list[dict]:
    pnrs = [r[0] for r in conn.execute(
        "SELECT b.pnr FROM bookings b JOIN passengers p ON p.id = b.passenger_id "
        "WHERE p.email = ? ORDER BY b.created_at DESC LIMIT 50", (email,))]
    return [get_booking(conn, p) for p in pnrs]


# --- Analytics --------------------------------------------------------------

def dashboard(conn) -> dict:
    expire_holds(conn)
    totals = dict(conn.execute(
        """
        SELECT (SELECT count(*) FROM flights WHERE departs_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')) AS upcoming_flights,
               (SELECT count(*) FROM bookings WHERE status = 'CONFIRMED') AS confirmed,
               (SELECT count(*) FROM bookings WHERE status = 'HELD') AS held,
               (SELECT round(sum(amount), 2) FROM payments) AS net_revenue,
               (SELECT round(avg(sold * 1.0 / capacity) * 100, 1) FROM v_flight_board) AS avg_load_pct
        """
    ).fetchone())
    top_routes = _rows(conn.execute("SELECT * FROM v_route_performance ORDER BY revenue_rank LIMIT 6"))
    hot = _rows(conn.execute(
        "SELECT flight_id, flight_no, origin, destination, departs_at, sold, capacity, "
        "round(sold * 100.0 / capacity, 1) AS load_pct FROM v_flight_board "
        "WHERE departs_at > strftime('%Y-%m-%dT%H:%M:%SZ','now') ORDER BY load_pct DESC LIMIT 6"
    ))
    audit = _rows(conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 10"))
    return {"totals": totals, "top_routes": top_routes, "hot_flights": hot, "recent_activity": audit}


_FORBIDDEN_ACTIONS = {
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_TRANSACTION,
}


def run_readonly_sql(db_path, sql: str, max_rows: int = 200, timeout_s: float = 3.0) -> dict:
    """Executes one SELECT on a read-only connection with three independent
    guards: mode=ro + query_only, an authorizer that denies every write action,
    and a progress handler that aborts long queries."""
    statement = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", statement):
        raise BookingError("Only a single SELECT (or WITH ... SELECT) statement is allowed.")
    if not sqlite3.complete_statement(statement + ";") or ";" in statement:
        raise BookingError("Exactly one statement, please.")

    conn = connect(db_path, readonly=True)
    conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in _FORBIDDEN_ACTIONS else sqlite3.SQLITE_OK)
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(statement)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.Error as e:
        raise BookingError(f"SQL error: {e}") from e
    finally:
        conn.close()
    return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}
