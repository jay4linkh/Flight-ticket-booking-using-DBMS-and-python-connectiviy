# SkyAgent — an agentic flight reservation DBMS

A flight booking system in Python + SQL where an AI agent (Claude) works the
database directly: it searches flights, reads seat maps, holds and books seats,
handles cancellations, and writes its own SQL for analytics questions. The
database enforces every business rule, so the agent, the web UI and the CLI all
run under the same guarantees.

```
 Browser UI ─┐                         ┌─ typed tools ─────────┐
 CLI chat  ──┼─► FastAPI ─► SkyAgent ──┤                       ├─► services.py ─► SQLite (WAL)
 REST      ──┘   (api.py)   (agent.py) └─ query_database ──────┘   (one shared      constraints, triggers,
                                          read-only sandbox          implementation)  views, FTS5, UDFs
```

## What makes it "advanced"

**DBMS layer (`skyagent/schema.sql`)**
- Normalized schema: airports, airlines, aircraft, routes, flights, per-flight seat inventory, passengers, bookings, payments, audit log.
- **No double bookings, enforced by the engine**: a *partial unique index* on `bookings(flight_id, seat_no) WHERE status IN ('HELD','CONFIRMED')`. The test suite runs 8 threads racing for one seat; exactly one wins.
- `BEGIN IMMEDIATE` transactions plus WAL mode for concurrent readers and serialized writers.
- **Triggers** for business rules: no bookings on departed or cancelled flights, a legal status state machine (`HELD → CONFIRMED → CANCELLED`, holds `EXPIRE`), loyalty points granted on confirm and clawed back on cancel, and a JSON **audit trail** that records whether a human (`web`/`cli`) or the `agent` acted.
- **Dynamic pricing as a SQL function**: `dynamic_fare(base, load_factor, days_out, cabin)` is a Python UDF registered with SQLite, so prices are computed inside queries.
- **FTS5 full-text search** over airports (`bombay` → BOM, `heathrow` → LHR).
- **Recursive CTEs** generate each flight's seat map, and the search query finds one-stop connections with a self-join (60 min to 8 h layovers).
- **Window functions** in the `v_route_performance` view rank routes by revenue.

**Agent layer (`skyagent/agent.py`)**
- Claude (`claude-opus-5-5`) with 9 tools: airport search, flight search, seat map, hold, confirm, cancel, seat change, booking lookup, and free-form SQL.
- The full database schema goes into the system prompt, so the agent can write correct SQL for questions no tool covers ("which routes will sell out this week?").
- **Safety by construction:**
  - Free-form SQL runs on a read-only connection (`mode=ro` + `query_only`), behind an authorizer that denies every write action, with a 3-second time limit.
  - Payment and cancellation tools refuse unless the traveller explicitly confirmed. Cancelling first returns the refund quote for the agent to relay.
  - Tool errors return to the model as `is_error` results so it can recover, for example by picking another seat.
- Every tool call the agent makes is shown in the UI as an expandable trace.

**App**
- Web UI with flight search, an interactive seat map, hold and pay, booking management, an operations dashboard, a SQL lab, and an agent chat.
- CLI: `seed`, `serve`, `chat`, `sql`.

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # macOS/Linux: .venv/bin/pip
python -m skyagent seed                              # builds skyagent.db (~1.6k flights, 150k bookings)
python -m skyagent serve                             # http://127.0.0.1:8000
```

The agent needs Claude API access: set `ANTHROPIC_API_KEY` before `serve` or `chat`.
Everything else works without it.

```bash
python -m skyagent chat
python -m skyagent sql "SELECT * FROM v_route_performance LIMIT 5"
python -m pytest -q
```

Environment variables: `SKYAGENT_DB` (database path), `SKYAGENT_MODEL` (default `claude-opus-5-5`).

## Example agent prompts

- *Cheapest way from Bangalore to London next Friday, flexible by a day*
- *Hold a window seat on the morning DEL→GOI flight for Asha Rao, asha@example.com*
- *Yes, pay with UPI*
- *Cancel booking K3J9QX — what would I get back?*
- *Which 5 flights this week have the highest load factor, and what are they charging now?*
- *Show me everything the agent changed today* (reads the audit log)
