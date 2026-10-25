"""Command line entry point.

    python -m skyagent seed           build the demo database
    python -m skyagent serve          web UI + API on http://127.0.0.1:8000
    python -m skyagent chat           talk to the agent in the terminal
    python -m skyagent sql "SELECT…"  run a read-only query
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import DEFAULT_DB
from .seed import seed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="skyagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("chat")
    q = sub.add_parser("sql")
    q.add_argument("query")
    args = p.parse_args(argv)

    seed(DEFAULT_DB)
    if args.cmd == "seed":
        print(f"Database ready at {DEFAULT_DB}")
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("skyagent.api:app", host=args.host, port=args.port)
    elif args.cmd == "sql":
        from .services import BookingError, run_readonly_sql
        try:
            r = run_readonly_sql(DEFAULT_DB, args.query)
        except BookingError as e:
            print(e, file=sys.stderr)
            return 1
        print("\t".join(r["columns"]))
        for row in r["rows"]:
            print("\t".join(map(str, row)))
    elif args.cmd == "chat":
        from .agent import SkyAgent
        agent = SkyAgent(DEFAULT_DB)
        print("SkyAgent ready. Ctrl+C to quit.")
        try:
            while True:
                text = input("\nyou> ").strip()
                if not text:
                    continue
                reply = agent.chat(text)
                for t in reply.trace:
                    print(f"  · {t['tool']}({json.dumps(t['input'])}){'  [error]' if t['error'] else ''}")
                print(f"\nagent> {reply.text}")
        except (KeyboardInterrupt, EOFError):
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
