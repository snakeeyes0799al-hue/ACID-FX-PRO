
from flask import Flask, jsonify, request, send_from_directory
import MetaTrader5 as mt5
import threading
import time
import math
import os
import secrets
import hmac
from pathlib import Path
from datetime import datetime, date

# ============================================================
# ACID FX PRO — PRIVATE DEMO/LIVE EXECUTION ENGINE
# ============================================================

app = Flask(__name__, static_folder=".", static_url_path="")

HOST = "127.0.0.1"
PORT = 8765
MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]

MAGIC = 25411011
COMMENT_PREFIX = "ACIDFXPRO"

RISK_PERCENT = 0.5
MAX_TRADES_PER_DAY = 3
MAX_OPEN_POSITIONS = 2
COOLDOWN_SECONDS = 600
REJECT_RETRY_SECONDS = 60
DEVIATION = 20

TRADE_EXECUTION_MARKET = 2
SYMBOL_FILLING_FOK_FLAG = 1
SYMBOL_FILLING_IOC_FLAG = 2

# ============================================================
# PRIVATE API TOKEN
# Stored outside the GitHub project folder.
# ============================================================

LOCAL_DATA = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home()))
)

TOKEN_DIR = LOCAL_DATA / "ACIDFXPRO"
TOKEN_FILE = TOKEN_DIR / "api_token.txt"

TOKEN_DIR.mkdir(parents=True, exist_ok=True)

if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(
        secrets.token_urlsafe(48),
        encoding="utf-8"
    )

API_TOKEN = TOKEN_FILE.read_text(
    encoding="utf-8"
).strip()

if len(API_TOKEN) < 40:
    raise RuntimeError(
        "API token file is invalid. "
        "Delete the local token file and restart."
    )

ALLOWED_ORIGINS = {
    "https://acid-fx-pro.vercel.app",
    "https://acidx.tailf72e0e.ts.net"
}

# ============================================================
# AUTHENTICATION AND CORS
# ============================================================

@app.before_request
def protect_api():

    if not request.path.startswith("/api/"):
        return None

    # Browser CORS preflight.
    if request.method == "OPTIONS":
        return "", 204

    supplied = request.headers.get(
        "Authorization", ""
    )

    expected = "Bearer " + API_TOKEN

    if not hmac.compare_digest(supplied, expected):
        return jsonify({
            "ok": False,
