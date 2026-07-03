"""Near-live state publishing to a GitHub Gist.

The committed paper_ledger/*.json files update once per run, but during a live
session the hosted dashboard wants fresher data. Each bot pushes a compact state
JSON to one shared Gist; the dashboard polls the gist's raw URL every ~30s.

Configuration (both optional — publishing is a no-op without them):
    GIST_TOKEN  a fine-grained PAT with only the "gists" scope
    GIST_ID     the id of the gist to update (create one file manually first)

Pushes are throttled (min interval) and never raise: a dashboard hiccup must
not take down a trading session.
"""

import json
import os
import time
import urllib.request

MIN_INTERVAL_S = 45          # don't PATCH the gist more often than this per file


class GistPublisher:
    def __init__(self, token: str | None = None, gist_id: str | None = None):
        self.token = token or os.getenv("GIST_TOKEN", "")
        self.gist_id = gist_id or os.getenv("GIST_ID", "")
        self._last_push: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.gist_id)

    def push(self, filename: str, state: dict, force: bool = False) -> bool:
        """PATCH one file in the gist with the JSON state. Returns True on success.
        Throttled per filename; silent no-op when unconfigured; never raises."""
        if not self.enabled:
            return False
        now = time.monotonic()
        if not force and now - self._last_push.get(filename, -1e9) < MIN_INTERVAL_S:
            return False
        try:
            body = json.dumps({"files": {filename: {
                "content": json.dumps(state)}}}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.github.com/gists/{self.gist_id}",
                data=body, method="PATCH",
                headers={"Authorization": f"Bearer {self.token}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json",
                         "User-Agent": "daytrade-bot"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = 200 <= resp.status < 300
            if ok:
                self._last_push[filename] = now
            return ok
        except Exception as e:            # noqa: BLE001 — never break a live session
            print(f"[gist] push failed ({e})")
            self._last_push[filename] = now   # back off; don't retry every bar
            return False
