-- SkyAgent schema: a normalized airline reservation database.
-- Integrity lives in the DBMS itself (constraints, partial unique indexes,
-- triggers), so every client -- the web UI, the CLI and the AI agent -- gets
-- the same guarantees.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS airports (
    iata     TEXT PRIMARY KEY CHECK (length(iata) = 3 AND iata = upper(iata)),
    name     TEXT NOT NULL,
    city     TEXT NOT NULL,
    country  TEXT NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL
);

-- Full-text index over airports so "bombay", "heathrow" or "new york" resolve.
CREATE VIRTUAL TABLE IF NOT EXISTS airports_fts USING fts5(
    iata, name, city, country, content='airports', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS airports_ai AFTER INSERT ON airports BEGIN
    INSERT INTO airports_fts(rowid, iata, name, city, country)
    VALUES (new.rowid, new.iata, new.name, new.city, new.country);
END;

CREATE TABLE IF NOT EXISTS airlines (
    code  TEXT PRIMARY KEY CHECK (length(code) = 2),
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS aircraft (
    id              INTEGER PRIMARY KEY,
    model           TEXT NOT NULL,
    rows            INTEGER NOT NULL CHECK (rows > 0),
    seats_per_row   INTEGER NOT NULL CHECK (seats_per_row BETWEEN 2 AND 10),
    business_rows   INTEGER NOT NULL DEFAULT 0 CHECK (business_rows >= 0)
);

CREATE TABLE IF NOT EXISTS routes (
    id           INTEGER PRIMARY KEY,
    origin       TEXT NOT NULL REFERENCES airports(iata),
    destination  TEXT NOT NULL REFERENCES airports(iata),
    airline      TEXT NOT NULL REFERENCES airlines(code),
    distance_km  INTEGER NOT NULL CHECK (distance_km > 0),
    base_fare    REAL NOT NULL CHECK (base_fare > 0),
    CHECK (origin <> destination),
    UNIQUE (origin, destination, airline)
);

CREATE TABLE IF NOT EXISTS flights (
    id           INTEGER PRIMARY KEY,
    flight_no    TEXT NOT NULL,
    route_id     INTEGER NOT NULL REFERENCES routes(id),
    aircraft_id  INTEGER NOT NULL REFERENCES aircraft(id),
    departs_at   TEXT NOT NULL,          -- ISO-8601 UTC
    arrives_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'SCHEDULED'
                 CHECK (status IN ('SCHEDULED', 'DELAYED', 'DEPARTED', 'CANCELLED')),
    CHECK (arrives_at > departs_at),
    UNIQUE (flight_no, departs_at)
);
CREATE INDEX IF NOT EXISTS ix_flights_route_time ON flights(route_id, departs_at);

CREATE TABLE IF NOT EXISTS seats (
    flight_id  INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    seat_no    TEXT NOT NULL,
    cabin      TEXT NOT NULL CHECK (cabin IN ('ECONOMY', 'BUSINESS')),
    is_window  INTEGER NOT NULL DEFAULT 0,
    is_aisle   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flight_id, seat_no)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS passengers (
    id              INTEGER PRIMARY KEY,
    full_name       TEXT NOT NULL CHECK (length(trim(full_name)) >= 2),
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (email LIKE '%_@_%._%'),
    loyalty_points  INTEGER NOT NULL DEFAULT 0 CHECK (loyalty_points >= 0),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS bookings (
    pnr              TEXT PRIMARY KEY CHECK (length(pnr) = 6),
    passenger_id     INTEGER NOT NULL REFERENCES passengers(id),
    flight_id        INTEGER NOT NULL,
    seat_no          TEXT NOT NULL,
    fare             REAL NOT NULL CHECK (fare > 0),
    status           TEXT NOT NULL DEFAULT 'HELD'
                     CHECK (status IN ('HELD', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
    hold_expires_at  TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (flight_id, seat_no) REFERENCES seats(flight_id, seat_no)
);
-- The double-booking guarantee: at most one live booking per seat, enforced by
-- the storage engine even under concurrent writers.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_live_seat
    ON bookings(flight_id, seat_no) WHERE status IN ('HELD', 'CONFIRMED');
CREATE INDEX IF NOT EXISTS ix_bookings_passenger ON bookings(passenger_id);
CREATE INDEX IF NOT EXISTS ix_bookings_flight_status ON bookings(flight_id, status);

CREATE TABLE IF NOT EXISTS payments (
    id        INTEGER PRIMARY KEY,
    pnr       TEXT NOT NULL REFERENCES bookings(pnr),
    amount    REAL NOT NULL,             -- negative for refunds
    method    TEXT NOT NULL CHECK (method IN ('CARD', 'UPI', 'WALLET', 'POINTS', 'REFUND')),
    paid_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    table_name TEXT NOT NULL,
    op         TEXT NOT NULL,
    row_key    TEXT NOT NULL,
    old_row    TEXT,                     -- JSON
    new_row    TEXT,                     -- JSON
    actor      TEXT,                     -- 'web', 'cli' or 'agent' via current_actor() UDF
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Business rules as triggers ------------------------------------------------

CREATE TRIGGER IF NOT EXISTS bookings_only_open_flights
BEFORE INSERT ON bookings
WHEN (SELECT status FROM flights WHERE id = new.flight_id) IN ('DEPARTED', 'CANCELLED')
  OR (SELECT departs_at FROM flights WHERE id = new.flight_id)
     <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
BEGIN
    SELECT RAISE(ABORT, 'flight is not open for booking');
END;

CREATE TRIGGER IF NOT EXISTS bookings_status_flow
BEFORE UPDATE OF status ON bookings
WHEN NOT (
       (old.status = 'HELD'      AND new.status IN ('CONFIRMED', 'CANCELLED', 'EXPIRED'))
    OR (old.status = 'CONFIRMED' AND new.status = 'CANCELLED')
)
BEGIN
    SELECT RAISE(ABORT, 'illegal booking status transition');
END;

-- Loyalty: 1 point per 10 currency units, granted on confirm, clawed back on cancel.
CREATE TRIGGER IF NOT EXISTS bookings_loyalty_earn
AFTER UPDATE OF status ON bookings
WHEN old.status = 'HELD' AND new.status = 'CONFIRMED'
BEGIN
    UPDATE passengers SET loyalty_points = loyalty_points + CAST(new.fare / 10 AS INTEGER)
    WHERE id = new.passenger_id;
END;

CREATE TRIGGER IF NOT EXISTS bookings_loyalty_clawback
AFTER UPDATE OF status ON bookings
WHEN old.status = 'CONFIRMED' AND new.status = 'CANCELLED'
BEGIN
    UPDATE passengers
    SET loyalty_points = max(0, loyalty_points - CAST(new.fare / 10 AS INTEGER))
    WHERE id = new.passenger_id;
END;

CREATE TRIGGER IF NOT EXISTS bookings_audit_insert
AFTER INSERT ON bookings
BEGIN
    INSERT INTO audit_log(table_name, op, row_key, new_row, actor)
    VALUES ('bookings', 'INSERT', new.pnr,
            json_object('status', new.status, 'flight_id', new.flight_id,
                        'seat_no', new.seat_no, 'fare', new.fare),
            current_actor());
END;

CREATE TRIGGER IF NOT EXISTS bookings_audit_update
AFTER UPDATE ON bookings
BEGIN
    INSERT INTO audit_log(table_name, op, row_key, old_row, new_row, actor)
    VALUES ('bookings', 'UPDATE', new.pnr,
            json_object('status', old.status, 'seat_no', old.seat_no),
            json_object('status', new.status, 'seat_no', new.seat_no),
            current_actor());
END;

-- Analytical views ----------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_flight_board AS
SELECT f.id            AS flight_id,
       f.flight_no,
       al.name         AS airline,
       r.origin,
       r.destination,
       f.departs_at,
       f.arrives_at,
       f.status,
       ac.model        AS aircraft,
       r.base_fare,
       r.distance_km,
       (SELECT count(*) FROM seats s WHERE s.flight_id = f.id) AS capacity,
       (SELECT count(*) FROM bookings b
         WHERE b.flight_id = f.id AND b.status IN ('HELD', 'CONFIRMED')) AS sold
FROM flights f
JOIN routes   r  ON r.id = f.route_id
JOIN airlines al ON al.code = r.airline
JOIN aircraft ac ON ac.id = f.aircraft_id;

CREATE VIEW IF NOT EXISTS v_route_performance AS
SELECT r.origin || '-' || r.destination AS route,
       r.airline,
       count(DISTINCT f.id)                                       AS flights,
       count(b.pnr)                                               AS confirmed_bookings,
       round(coalesce(sum(b.fare), 0), 2)                         AS revenue,
       rank() OVER (ORDER BY coalesce(sum(b.fare), 0) DESC)       AS revenue_rank
FROM routes r
JOIN flights f ON f.route_id = r.id
LEFT JOIN bookings b ON b.flight_id = f.id AND b.status = 'CONFIRMED'
GROUP BY r.id;
