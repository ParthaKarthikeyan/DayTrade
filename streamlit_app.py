import os
import time
import streamlit as st

from broker.oanda_client import OandaClient
from price_action.patterns import validate_signal
from utils.logger import get_logger

LOG_FILE = os.path.join("logs", "app.log")
logger = get_logger("ui")

st.set_page_config(page_title="TradingView -> OANDA (Streamlit)", layout="wide")

st.title("TradingView -> OANDA Controller")

with st.sidebar:
	st.header("Connection")
	env_info = {
		"OANDA_ENV": os.getenv("OANDA_ENV", "practice"),
		"USE_OANDA_V20": os.getenv("USE_OANDA_V20", "false"),
		"ACCOUNT": os.getenv("OANDA_ACCOUNT_ID", "(env not set)"),
	}
	st.code("\n".join(f"{k} = {v}" for k, v in env_info.items()))
	st.divider()
	st.caption("Logs: logs/app.log")

	refresh = st.button("Refresh Status")

if "client" not in st.session_state:
	try:
		st.session_state.client = OandaClient()
	except Exception as e:
		st.error(f"Broker init failed: {e}")
		st.stop()

client: OandaClient = st.session_state.client

col1, col2 = st.columns(2)
with col1:
	st.subheader("Account Status")
	if refresh:
		pass
	try:
		acct = client.get_account_summary()
		st.success("Connected to OANDA")
		st.json({
			"id": acct.get("id"),
			"balance": acct.get("balance"),
			"currency": acct.get("currency"),
		})
	except Exception as e:
		st.error(f"Status failed: {e}")

with col2:
	st.subheader("Place Market Order")
	with st.form("trade_form", clear_on_submit=False):
		symbol = st.text_input("Instrument (e.g., EUR_USD)", value="EUR_USD")
		side = st.selectbox("Side", options=["buy", "sell"], index=0)
		entry_price = st.number_input("Entry Price", value=1.10000, format="%.5f")
		stop_loss = st.number_input("Stop Loss Price", value=1.09800, format="%.5f")
		take_profit = st.number_input("Take Profit Price (optional)", value=1.10400, format="%.5f")
		risk_pct = st.number_input("Risk % of Balance", value=0.5, min_value=0.0, max_value=100.0, step=0.1)
		submit = st.form_submit_button("Submit Order")

	if submit:
		payload = {
			"symbol": symbol,
			"side": side,
			"entry_price": float(entry_price),
			"stop_loss": float(stop_loss),
			"take_profit": float(take_profit) if take_profit else None,
			"risk_pct": float(risk_pct),
		}
		st.write("Validating signal…")
		if not validate_signal(payload):
			st.warning("Order blocked by price-action validation.")
		else:
			try:
				units = client.calculate_units(
					symbol=symbol,
					entry_price=float(entry_price),
					stop_loss=float(stop_loss),
					desired_risk_pct=float(risk_pct),
				)
				st.info(f"Placing {side} {symbol} with {units} units…")
				result = client.place_market_order(
					symbol=symbol,
					side=side,
					units=units,
					stop_loss=float(stop_loss),
					take_profit=float(take_profit) if take_profit else None,
				)
				st.success("Order placed")
				st.json(result)
			except Exception as e:
				logger.exception("Order failed via UI")
				st.error(f"Order failed: {e}")

st.subheader("Live Logs")
log_container = st.empty()

def tail_log(path: str, lines: int = 200) -> str:
	if not os.path.exists(path):
		return "(no log file yet)"
	with open(path, "r", encoding="utf-8", errors="ignore") as f:
		data = f.readlines()[-lines:]
	return "".join(data)

logs_text = tail_log(LOG_FILE, 300)
st.text_area("logs/app.log", logs_text, height=300)
