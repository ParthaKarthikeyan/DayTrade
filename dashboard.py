#!/usr/bin/env python3
"""Self-contained local dashboard for tracking the bot (live paper + backtests).

The bot writes logs/state.json; this Flask app serves a single auto-refreshing
HTML page (no external CDNs) plus a /api/state JSON endpoint the page polls.

    python run_backtest.py --symbol AAPL --date 2026-06-26   # writes state
    python dashboard.py                                       # open localhost:5000
"""

import argparse
import json
import os

from flask import Flask, jsonify, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "static", "dashboard.html")

app = Flask(__name__)
app.config["STATE_PATH"] = os.path.join("logs", "state.json")


@app.get("/")
def index():
    return send_file(PAGE)


@app.get("/api/state")
def api_state():
    path = app.config["STATE_PATH"]
    if not os.path.exists(path):
        return jsonify({})
    try:
        with open(path, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (json.JSONDecodeError, OSError):
        # File may be mid-write; the page will retry on its next poll.
        return jsonify({})


def main():
    p = argparse.ArgumentParser(description="DayTrade live dashboard")
    p.add_argument("--state", default=os.path.join("logs", "state.json"),
                   help="path to the state JSON written by the bot")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    app.config["STATE_PATH"] = args.state
    print(f"Dashboard on http://{args.host}:{args.port}  (state: {args.state})")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
