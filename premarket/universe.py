"""Default scan universe for the premarket backtest.

IMPORTANT LIMITATION: a true scanner sees the *entire* market each morning.
There is no free historical "top gappers" feed, so the backtest reconstructs
gaps from daily bars over this fixed list. That means the backtest only ever
"sees" names on this list — it will miss one-off news gappers that aren't here,
so treat the results as indicative, not exhaustive. Expand it with --universe-file
(one ticker per line) for broader coverage. The LIVE bot has no such limit: it
uses Alpaca's whole-market movers screener.

This list favors liquid, frequently-active low-priced / high-beta names that
commonly show up in small-cap momentum scans.
"""

DEFAULT_UNIVERSE = [
    # High-beta / frequently-gapping liquid names (price varies; band filter applies)
    "SOFI", "PLUG", "NIO", "LCID", "RIVN", "F", "BBAI", "SOUN", "PLTR", "MARA",
    "RIOT", "CLSK", "HUT", "BITF", "WULF", "IREN", "CIFR", "AI", "CHPT", "RUN",
    "FCEL", "BLNK", "QS", "NKLA", "GOEV", "RIDE", "WKHS", "HYLN", "PSNY", "EVGO",
    "AMC", "GME", "BBBYQ", "CVNA", "UPST", "AFRM", "OPEN", "WISH", "CLOV", "SDC",
    "DKNG", "HOOD", "RBLX", "COIN", "SNAP", "PINS", "LYFT", "U", "PATH", "DNA",
    "RKLB", "ASTS", "SPCE", "ACHR", "JOBY", "LAZR", "VLDR", "MVIS", "OUST", "INDI",
    "TLRY", "CGC", "ACB", "SNDL", "HEXO", "OGI", "CRON", "VFF", "GRWG", "SMCI",
    "IONQ", "QBTS", "RGTI", "ARQQ", "LAES", "MULN", "FFIE", "NVAX", "OCGN", "INO",
    "VTGN", "ATER", "PROG", "BBIG", "DWAC", "PHUN", "MMAT", "CEI", "GNS", "HKD",
    "TTOO", "SNGX", "AYTU", "CTXR", "ENVB", "BNGO", "PACB", "EDIT", "BEAM", "NTLA",
]
