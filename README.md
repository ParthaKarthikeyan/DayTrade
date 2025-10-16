# TradingView Forex Auto-Trader (Python + OANDA)

A minimal, production-ready starter to receive TradingView webhooks and place Forex trades via OANDA with basic risk controls and a price-action hook you can extend.

## Quick start

1) Create and fill a `.env` file (copy from `.env.example`):

```
OANDA_API_KEY=YOUR_OANDA_V20_TOKEN
OANDA_ACCOUNT_ID=YOUR_OANDA_ACCOUNT_ID
OANDA_ENV=practice
WEBHOOK_PORT=8080
WEBHOOK_HOST=0.0.0.0
ALERT_SHARED_SECRET=change-this
MAX_RISK_PCT=1.0
```

2) Install dependencies:

```bash
pip install -r requirements.txt
```

3) Run the webhook server:

```bash
python app.py
```

4) In TradingView, set an alert with Webhook URL pointing to your server, e.g. `http://<your-ip>:8080/webhook`.

### TradingView alert payload
Set the alert message to valid JSON like this (edit values as needed):

```json
{
  "secret": "change-this",
  "strategy_id": "price_action_v1",
  "symbol": "EUR_USD",
  "side": "buy",
  "entry_price": 1.1005,
  "stop_loss": 1.0985,
  "take_profit": 1.1045,
  "risk_pct": 0.5,
  "timeframe": "15"
}
```

- `symbol` must use OANDA instrument symbols like `EUR_USD`, `GBP_JPY`, etc.
- `risk_pct` is percent of account balance to risk on this trade. Capped by `MAX_RISK_PCT`.
- Server assumes account currency equals the quote currency. If not, adjust the risk model in `broker/oanda_client.py`.

## Files
- `app.py`: Flask webhook that validates and routes orders
- `config.py`: Environment management and constants
- `broker/oanda_client.py`: Thin OANDA REST client and order sizing
- `price_action/patterns.py`: Hook for your price-action filters
- `utils/logger.py`: Structured logging to console and file
- `requirements.txt`: Python deps

## Security
- Uses a shared secret in the JSON payload (`secret`). Match it to `ALERT_SHARED_SECRET`.
- Restrict inbound IPs at your firewall/reverse proxy if exposing publicly.

## Extending price action
Implement your logic in `price_action/patterns.py`. The server calls `validate_signal(...)` before placing orders. Return `False` to block trades that do not meet your criteria.

## Pine Script alert example
Put this in your TradingView alert message (JSON), and keep it in sync with your strategy variables:

```json
{
  "secret": "change-this",
  "strategy_id": "price_action_v1",
  "symbol": "{{ticker}}".replace(":", "_"),
  "side": "{{strategy.order.action}}".toLowerCase(),
  "entry_price": {{close}},
  "stop_loss": {{close}} - 0.0020,
  "take_profit": {{close}} + 0.0040,
  "risk_pct": 0.5,
  "timeframe": "{{interval}}"
}
```

Note: Pine cannot compute HMAC headers dynamically in alerts; use the shared secret field.

## Disclaimer
This code is for educational purposes. Trading involves risk. Use at your own risk and test thoroughly on paper or demo accounts before going live.

