"""QQQ 1-minute history from Alpaca's IEX feed (free tier, deep history).

Returns an ET-indexed OHLCV frame trimmed to 08:00-16:00 — IEX's premarket
open through the close. The premarket slice feeds level construction; the
09:30+ slice is what the strategy trades.
"""

import os
import time
from typing import Optional

import pandas as pd
import requests

DATA_URL = "https://data.alpaca.markets"
ET = "America/New_York"


def _headers() -> dict:
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def get_minute_bars(symbol: str, start: str, end: str,
                    cache: Optional[str] = None) -> pd.DataFrame:
    """Paginated 1-minute IEX bars, ET-indexed, 08:00-16:00 only.
    `start`/`end` are ISO dates. Optional pickle cache path (local reruns)."""
    if cache and os.path.exists(cache):
        return pd.read_pickle(cache)

    rows, token = [], None
    while True:
        params = {"symbols": symbol, "timeframe": "1Min",
                  "start": f"{start}T08:00:00-05:00", "end": f"{end}T21:00:00-05:00",
                  "feed": "iex", "adjustment": "raw", "limit": 10000}
        if token:
            params["page_token"] = token
        for attempt in range(5):
            r = requests.get(f"{DATA_URL}/v2/stocks/bars", headers=_headers(),
                             params=params, timeout=30)
            if r.status_code == 429:            # rate limit: back off and retry
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        out = r.json()
        rows += out.get("bars", {}).get(symbol, [])
        token = out.get("next_page_token")
        if not token:
            break

    if not rows:
        raise RuntimeError(f"Alpaca returned no 1m bars for {symbol}")
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"]).dt.tz_convert(ET)
    df = (df.set_index("t").sort_index()
            .rename(columns={"o": "open", "h": "high", "l": "low",
                             "c": "close", "v": "volume"})
            [["open", "high", "low", "close", "volume"]]
            .between_time("08:00", "16:00"))
    df = df[df.index.dayofweek < 5]
    if cache:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        df.to_pickle(cache)
    return df
