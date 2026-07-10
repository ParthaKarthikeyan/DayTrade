"""Opening-hour sweep-reversal strategy on QQQ (NQ-futures translation).

Source idea: "The NEW 1 Minute Scalping Strategy" video — premark S/R zones
on a 15-minute chart (overnight high/low, prior-day levels, swing extremes),
then in the opening session fade FAILED BREAKOUTS of those levels on the
1-minute chart: price sweeps through a level, snaps back with a strong body,
stop beyond the sweep extreme, target a multiple of the risk.

Translation notes (why QQQ, not NQ futures):
- No futures broker on this stack; Alpaca trades QQQ long/short with ~0.2 bps
  half-spreads — futures-grade costs on the same NASDAQ-100 exposure.
- Free 1-minute NQ history no longer exists (Yahoo caps 1m at ~8 days/request);
  Alpaca IEX serves years of QQQ 1-minute bars including premarket (from 08:00
  ET, IEX's premarket open — the 04:00-08:00 overnight tape is invisible, so
  "overnight high/low" here honestly means the 08:00-09:30 premarket range,
  plus prior-day levels which IEX covers fully).
"""
