"""Builds a realistic demo network: airports, fictional airlines, a schedule
for the next few weeks, seat inventory and some existing demand."""

from __future__ import annotations

import math
import random
import string
from datetime import datetime, timedelta, timezone

from .db import ISO, connect, init_schema, transaction, DEFAULT_DB

AIRPORTS = [
    ("DEL", "Indira Gandhi International", "New Delhi", "India", 28.556, 77.100),
    ("BOM", "Chhatrapati Shivaji Maharaj International", "Mumbai (Bombay)", "India", 19.089, 72.865),
    ("BLR", "Kempegowda International", "Bengaluru (Bangalore)", "India", 13.199, 77.706),
    ("MAA", "Chennai International", "Chennai (Madras)", "India", 12.994, 80.170),
    ("CCU", "Netaji Subhas Chandra Bose International", "Kolkata (Calcutta)", "India", 22.654, 88.446),
    ("HYD", "Rajiv Gandhi International", "Hyderabad", "India", 17.231, 78.429),
    ("GOI", "Manohar International", "Goa", "India", 15.740, 73.860),
    ("DXB", "Dubai International", "Dubai", "United Arab Emirates", 25.253, 55.365),
    ("SIN", "Changi", "Singapore", "Singapore", 1.364, 103.991),
    ("LHR", "Heathrow", "London", "United Kingdom", 51.470, -0.454),
    ("JFK", "John F. Kennedy International", "New York", "United States", 40.641, -73.778),
    ("SFO", "San Francisco International", "San Francisco", "United States", 37.621, -122.379),
    ("FRA", "Frankfurt am Main", "Frankfurt", "Germany", 50.038, 8.562),
    ("NRT", "Narita International", "Tokyo", "Japan", 35.772, 140.393),
    ("SYD", "Kingsford Smith", "Sydney", "Australia", -33.940, 151.175),
    ("CDG", "Charles de Gaulle", "Paris", "France", 49.010, 2.548),
]

# Fictional carriers, so the demo never impersonates a real airline.
AIRLINES = [
    ("SA", "Saffron Air"),
    ("NV", "Nova Airways"),
    ("MX", "Meridian Express"),
    ("AQ", "Aquila International"),
]

AIRCRAFT = [
    # model, rows, seats_per_row, business_rows
    ("A320neo", 30, 6, 2),
    ("B737 MAX 8", 32, 6, 3),
    ("A350-900", 40, 9, 6),
    ("B787-9", 38, 9, 5),
]

DOMESTIC = {"DEL", "BOM", "BLR", "MAA", "CCU", "HYD", "GOI"}

ROUTE_PAIRS = [
    ("DEL", "BOM"), ("DEL", "BLR"), ("DEL", "CCU"), ("DEL", "HYD"), ("DEL", "GOI"),
    ("BOM", "BLR"), ("BOM", "MAA"), ("BOM", "GOI"), ("BLR", "MAA"), ("BLR", "HYD"),
    ("CCU", "BLR"), ("DEL", "DXB"), ("BOM", "DXB"), ("BLR", "SIN"), ("DEL", "LHR"),
    ("BOM", "LHR"), ("DEL", "JFK"), ("BLR", "SFO"), ("DEL", "FRA"), ("DEL", "NRT"),
    ("SIN", "SYD"), ("DXB", "LHR"), ("LHR", "JFK"), ("CDG", "JFK"), ("BOM", "CDG"),
    ("MAA", "SIN"), ("FRA", "SFO"),
]

FIRST_NAMES = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Isha", "Arjun", "Saanvi",
               "Liam", "Emma", "Noah", "Olivia", "Yuki", "Chen", "Fatima", "Omar", "Sofia", "Lucas"]
LAST_NAMES = ["Sharma", "Iyer", "Gupta", "Reddy", "Khan", "Patel", "Das", "Nair", "Singh", "Mehta",
              "Smith", "Garcia", "Tanaka", "Wang", "Haddad", "Rossi", "Müller", "Brown"]


def haversine_km(a, b) -> int:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return int(2 * 6371 * math.asin(math.sqrt(h)))


def new_pnr(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=6))


def generate_seats(conn, flight_id: int, aircraft_id: int) -> None:
    """Expands an aircraft's seat map with a recursive CTE -- one INSERT per flight."""
    conn.execute(
        """
        WITH RECURSIVE
          ac AS (SELECT rows, seats_per_row AS spr, business_rows FROM aircraft WHERE id = :ac),
          r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r, ac WHERE n < ac.rows),
          c(k) AS (SELECT 1 UNION ALL SELECT k + 1 FROM c, ac WHERE k < ac.spr)
        INSERT INTO seats(flight_id, seat_no, cabin, is_window, is_aisle)
        SELECT :f,
               r.n || substr('ABCDEFGHJK', c.k, 1),
               CASE WHEN r.n <= ac.business_rows THEN 'BUSINESS' ELSE 'ECONOMY' END,
               c.k IN (1, ac.spr),
               CASE ac.spr WHEN 6 THEN c.k IN (3, 4) WHEN 9 THEN c.k IN (3, 4, 6, 7) ELSE 0 END
        FROM r, c, ac
        """,
        {"ac": aircraft_id, "f": flight_id},
    )


def seed(path=DEFAULT_DB, days: int = 21, seed_value: int = 7, demand: bool = True) -> None:
    rng = random.Random(seed_value)
    conn = connect(path, actor="seed")
    init_schema(conn)
    if conn.execute("SELECT count(*) FROM airports").fetchone()[0]:
        return  # already seeded

    coords = {a[0]: (a[4], a[5]) for a in AIRPORTS}
    with transaction(conn):
        conn.executemany("INSERT INTO airports VALUES (?,?,?,?,?,?)", AIRPORTS)
        conn.executemany("INSERT INTO airlines VALUES (?,?)", AIRLINES)
        conn.executemany(
            "INSERT INTO aircraft(model, rows, seats_per_row, business_rows) VALUES (?,?,?,?)", AIRCRAFT
        )

        routes = []
        for i, (o, d) in enumerate(ROUTE_PAIRS):
            km = haversine_km(coords[o], coords[d])
            base = round(35 + 0.075 * km, 2)
            for orig, dest in ((o, d), (d, o)):
                airline = AIRLINES[i % len(AIRLINES)][0]
                cur = conn.execute(
                    "INSERT INTO routes(origin, destination, airline, distance_km, base_fare) VALUES (?,?,?,?,?)",
                    (orig, dest, airline, km, base),
                )
                routes.append((cur.lastrowid, orig, dest, airline, km))

        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        flight_counter = 100
        for route_id, orig, dest, airline, km in routes:
            long_haul = not (orig in DOMESTIC and dest in DOMESTIC)
            aircraft_id = rng.choice([3, 4]) if km > 3000 else rng.choice([1, 2])
            daily = [rng.randint(5, 9), rng.randint(13, 20)] if not long_haul else [rng.randint(1, 23)]
            flight_counter += 7
            duration = timedelta(minutes=int(40 + km / 13.5))
            for day in range(days):
                for leg, hour in enumerate(daily):
                    dep = start + timedelta(days=day, hours=hour, minutes=rng.choice([0, 15, 30, 45]))
                    cur = conn.execute(
                        "INSERT INTO flights(flight_no, route_id, aircraft_id, departs_at, arrives_at) "
                        "VALUES (?,?,?,?,?)",
                        (f"{airline}{flight_counter + leg}", route_id, aircraft_id,
                         dep.strftime(ISO), (dep + duration).strftime(ISO)),
                    )
                    generate_seats(conn, cur.lastrowid, aircraft_id)

        if demand:
            _seed_demand(conn, rng)
    conn.execute("ANALYZE")  # give the query planner real statistics


def _seed_demand(conn, rng: random.Random) -> None:
    people = []
    for i in range(400):
        fn, ln = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        cur = conn.execute(
            "INSERT INTO passengers(full_name, email, loyalty_points) VALUES (?,?,?)",
            (f"{fn} {ln}", f"{fn.lower()}.{ln.lower()}{i}@example.com", rng.randint(0, 4000)),
        )
        people.append(cur.lastrowid)

    flights = conn.execute(
        "SELECT f.id, f.departs_at, r.base_fare FROM flights f JOIN routes r ON r.id = f.route_id"
    ).fetchall()
    used = set()
    rows = []
    for f in flights:
        seats = [r[0] for r in conn.execute("SELECT seat_no FROM seats WHERE flight_id = ?", (f["id"],))]
        # Nearer departures are fuller; a few "hot" flights are nearly sold out.
        days_out = (datetime.strptime(f["departs_at"], ISO).replace(tzinfo=timezone.utc)
                    - datetime.now(timezone.utc)).days
        target = max(0.05, 0.75 - days_out * 0.03) * rng.uniform(0.5, 1.2)
        if rng.random() < 0.04:
            target = 0.97
        for seat in rng.sample(seats, int(len(seats) * min(target, 0.98))):
            pnr = new_pnr(rng)
            while pnr in used:
                pnr = new_pnr(rng)
            used.add(pnr)
            rows.append((pnr, rng.choice(people), f["id"], seat, round(f["base_fare"] * rng.uniform(0.9, 2.2), 2)))
    conn.executemany(
        "INSERT INTO bookings(pnr, passenger_id, flight_id, seat_no, fare, status) VALUES (?,?,?,?,?,'CONFIRMED')",
        rows,
    )
    conn.execute(
        "INSERT INTO payments(pnr, amount, method) SELECT pnr, fare, 'CARD' FROM bookings"
    )
    conn.execute("DELETE FROM audit_log")  # seed data is not interesting history


if __name__ == "__main__":
    seed()
    print(f"Seeded {DEFAULT_DB}")
