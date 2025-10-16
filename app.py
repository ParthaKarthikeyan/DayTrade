from flask import Flask, request, jsonify

from config import ALERT_SHARED_SECRET, WEBHOOK_HOST, WEBHOOK_PORT
from broker.oanda_client import OandaClient
from price_action.patterns import validate_signal
from utils.logger import get_logger
from utils.sanitize import sanitize_payload

app = Flask(__name__)
logger = get_logger("webhook")

oanda = None  # lazy init


@app.get("/")
def index():
	logger.debug("GET / called")
	return jsonify({
		"status": "running",
		"message": "Use POST /webhook from TradingView",
	}), 200


@app.get("/health")
def health():
	logger.debug("GET /health called")
	return jsonify({"ok": True}), 200


@app.get("/status")
def status():
	global oanda
	logger.info("GET /status called: initializing client if needed")
	try:
		if oanda is None:
			logger.debug("Creating OandaClient in /status")
			oanda = OandaClient()
		acct = oanda.get_account_summary()
		logger.info("/status ok: id=%s currency=%s", acct.get("id"), acct.get("currency"))
		return jsonify({
			"ok": True,
			"account": {
				"id": acct.get("id"),
				"balance": acct.get("balance"),
				"currency": acct.get("currency"),
			},
		}), 200
	except Exception as e:
		logger.exception("/status failed")
		return jsonify({"ok": False, "error": str(e)}), 400


@app.get("/webhook")
def webhook_get():
	logger.debug("GET /webhook called")
	return jsonify({"message": "Use POST for alerts"}), 200


@app.post("/webhook")
def webhook():
	logger.info("POST /webhook received")
	if not request.is_json:
		logger.warning("/webhook: non-JSON request")
		return jsonify({"error": "Expected JSON"}), 400
	payload = request.get_json(silent=True) or {}
	logger.debug("/webhook: payload=%s", sanitize_payload(payload))
	if payload.get("secret") != ALERT_SHARED_SECRET:
		logger.warning("/webhook: bad secret")
		return jsonify({"error": "Unauthorized"}), 401

	required = ["symbol", "side", "entry_price", "stop_loss"]
	missing = [k for k in required if k not in payload]
	if missing:
		logger.warning("/webhook: missing fields: %s", missing)
		return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

	logger.info("/webhook: validating price action for %s %s", payload.get("side"), payload.get("symbol"))
	if not validate_signal(payload):
		logger.info("/webhook: blocked by price_action")
		return jsonify({"status": "blocked_by_price_action"}), 200

	global oanda
	if oanda is None:
		try:
			logger.debug("/webhook: creating OandaClient")
			oanda = OandaClient()
		except Exception as e:
			logger.error("/webhook: OandaClient init failed: %s", e)
			return jsonify({"error": str(e)}), 400

	try:
		logger.info("/webhook: sizing position")
		units = oanda.calculate_units(
			symbol=str(payload["symbol"]),
			entry_price=float(payload["entry_price"]),
			stop_loss=float(payload["stop_loss"]),
			desired_risk_pct=float(payload.get("risk_pct", 0.5)),
		)
		logger.info("/webhook: placing order units=%s", units)
		result = oanda.place_market_order(
			symbol=str(payload["symbol"]),
			side=str(payload["side"]),
			units=units,
			stop_loss=float(payload["stop_loss"]),
			take_profit=float(payload["take_profit"]) if payload.get("take_profit") is not None else None,
		)
		logger.info("/webhook: order placed successfully")
		return jsonify({"status": "ok", "units": units, "broker_response": result}), 200
	except Exception as e:
		logger.exception("/webhook: order failed")
		return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
	logger.info("Starting webhook on %s:%s", WEBHOOK_HOST, WEBHOOK_PORT)
	app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)

