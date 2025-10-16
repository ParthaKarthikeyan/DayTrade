import os
from dotenv import load_dotenv

load_dotenv()

OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
ALERT_SHARED_SECRET = os.getenv("ALERT_SHARED_SECRET", "change-this")

MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "1.0"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.getenv("LOG_DIR", "logs")

USE_OANDA_V20 = os.getenv("USE_OANDA_V20", "false").strip().lower() in {"1", "true", "yes", "on"}

# Derived
OANDA_API_BASE = (
    "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"
)

REQUIRED_CONFIG = [
    ("OANDA_API_KEY", OANDA_API_KEY),
    ("OANDA_ACCOUNT_ID", OANDA_ACCOUNT_ID),
]

