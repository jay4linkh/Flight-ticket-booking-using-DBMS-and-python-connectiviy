import json
import threading
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from skyagent import services as svc
from skyagent.agent import SkyAgent
from skyagent.db import connect
from skyagent.seed import seed


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "test.db"
    seed(path, days=4, demand=False)
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path, actor="test")
    yield c
    c.close()


def in_days(n):
    return (date.today() + timedelta(days=n)).isoformat()


def first_flight(conn, o="DEL", d="BOM"):
    return svc.search_flights(conn, o, d, in_days(2), flex_days=1)[0]["legs"][0]["flight_id"]


def test_airport_full_text_search(conn):
    assert svc.resolve_airport(conn, "bombay") == "BOM"
    assert svc.resolve_airport(conn, "heathrow") == "LHR"
    with pytest.raises(svc.BookingError):
        svc.resolve_airport(conn, "atlantis")


def test_connections_found_when_no_direct_route(conn):
    its = svc.search_flights(conn, "BLR", "LHR", in_days(2), flex_days=1)
    assert its and all(i["stops"] == 1 for i in its)
    for i in its:
        a, b = i["legs"]
        assert a["destination"] == b["origin"]
        assert 60 <= i["layover_minutes"] <= 480


def test_full_lifecycle_with_loyalty_and_audit(conn):
    fid = first_flight(conn)
    held = svc.hold_seat(conn, fid, "Test Traveller", "life@example.com", preference="WINDOW")
    assert held["status"] == "HELD" and held["seat_no"][-1] in "AF"
    confirmed = svc.confirm_booking(conn, held["pnr"], "UPI")
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["loyalty_points"] == int(held["fare"] / 10)
    cancelled = svc.cancel_booking(conn, held["pnr"])
    assert cancelled["status"] == "CANCELLED" and cancelled["loyalty_points"] == 0
    ops = [r["new_row"] for r in conn.execute(
        "SELECT new_row FROM audit_log WHERE row_key = ? ORDER BY id", (held["pnr"],))]
    assert [json.loads(o)["status"] for o in ops] == ["HELD", "CONFIRMED", "CANCELLED"]
    assert conn.execute("SELECT DISTINCT actor FROM audit_log WHERE row_key = ?", (held["pnr"],)).fetchone()[0] == "test"


def test_illegal_transition_blocked_by_trigger(conn):
    fid = first_flight(conn)
    b = svc.hold_seat(conn, fid, "Trigger Test", "trig@example.com")
    svc.cancel_booking(conn, b["pnr"])
    with pytest.raises(Exception, match="illegal booking status transition"):
        conn.execute("UPDATE bookings SET status = 'CONFIRMED' WHERE pnr = ?", (b["pnr"],))


def test_concurrent_holds_on_one_seat_only_one_wins(db_path):
    c = connect(db_path)
    fid = first_flight(c)
    seat = svc.seat_map(c, fid)["seats"][-1]["seat_no"]
    results, barrier = [], threading.Barrier(8)

    def worker(i):
        wc = connect(db_path, actor=f"w{i}")
        barrier.wait()
        try:
            svc.hold_seat(wc, fid, f"Racer {i}", f"racer{i}@example.com", seat_no=seat)
            results.append("ok")
        except svc.BookingError:
            results.append("taken")
        finally:
            wc.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1 and results.count("taken") == 7


def test_dynamic_fare_rises_with_load():
    from skyagent.db import dynamic_fare
    assert dynamic_fare(100, 0.9, 30, "ECONOMY") > dynamic_fare(100, 0.1, 30, "ECONOMY")
    assert dynamic_fare(100, 0.5, 1, "ECONOMY") > dynamic_fare(100, 0.5, 30, "ECONOMY")
    assert dynamic_fare(100, 0.5, 10, "BUSINESS") > dynamic_fare(100, 0.5, 10, "ECONOMY")


@pytest.mark.parametrize("sql", [
    "DELETE FROM bookings",
    "SELECT 1; DROP TABLE bookings",
    "WITH x AS (SELECT 1) INSERT INTO airlines VALUES ('ZZ', 'Evil')",
    "PRAGMA writable_schema = 1",
    "ATTACH DATABASE 'x.db' AS x",
])
def test_sql_sandbox_rejects_writes(db_path, sql):
    with pytest.raises(svc.BookingError):
        svc.run_readonly_sql(db_path, sql)


def test_sql_sandbox_allows_analytics(db_path):
    r = svc.run_readonly_sql(db_path, "SELECT route, revenue_rank FROM v_route_performance LIMIT 3")
    assert r["columns"] == ["route", "revenue_rank"] and len(r["rows"]) == 3


class FakeClient:
    """Replays scripted model turns so the tool loop can be tested offline."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.turns.pop(0)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def text(t):
    return SimpleNamespace(type="text", text=t)


def test_agent_loop_runs_tools_and_guards_payment(db_path):
    c = connect(db_path)
    fid = first_flight(c)
    c.close()
    client = FakeClient([
        SimpleNamespace(stop_reason="tool_use", content=[
            tool_use("t1", "hold_seat", {"flight_id": fid, "full_name": "Agent User", "email": "agent@example.com"}),
        ]),
        SimpleNamespace(stop_reason="tool_use", content=[
            tool_use("t2", "confirm_booking", {"pnr": "XXXXXX", "payment_method": "CARD", "traveller_confirmed": False}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[text("Held your seat; confirm to pay.")]),
    ])
    agent = SkyAgent(db_path, client=client)
    reply = agent.chat("Book me the first flight")
    assert reply.text == "Held your seat; confirm to pay."
    assert [t["tool"] for t in reply.trace] == ["hold_seat", "confirm_booking"]
    assert not reply.trace[0]["error"] and reply.trace[1]["error"]
    assert "confirm the fare" in reply.trace[1]["output"]
    # Tool results go back in one user message per round, matched by id.
    # History: user, assistant(t1), results(t1), assistant(t2), results(t2), assistant(final).
    last_results = agent.messages[-2]["content"]
    assert last_results[0]["tool_use_id"] == "t2" and last_results[0]["is_error"] is True
    assert client.calls[0]["fallbacks"] == "default"
