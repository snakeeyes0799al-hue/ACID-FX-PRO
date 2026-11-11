
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
# ACID FX PRO — PRIVATE DEMO-ONLY EXECUTION ENGINE
# ============================================================

app = Flask(__name__, static_folder=".", static_url_path="")

HOST = "127.0.0.1"
PORT = 8765
MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]

MAGIC = 25411011
COMMENT = "ACIDFXPRO_DEMO"

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
            "message": "Unauthorized. Enter your private API token."
        }), 401

    return None


@app.after_request
def add_cors_headers(response):

    origin = request.headers.get("Origin", "")

    if origin in ALLOWED_ORIGINS:

        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization"
        )
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, OPTIONS"
        )
        response.headers["Vary"] = "Origin"

    return response


# ============================================================
# STRATEGIES
# ============================================================

BOTS = {
    "SCALPER": {
        "label": "ACID SCALPER",
        "entry_tf": mt5.TIMEFRAME_M1,
        "trend_tf": mt5.TIMEFRAME_M5,
        "sl_atr": 1.5,
        "rr": 1.5
    },
    "NORMAL": {
        "label": "ACID NORMAL TRADER",
        "entry_tf": mt5.TIMEFRAME_M5,
        "trend_tf": mt5.TIMEFRAME_M15,
        "sl_atr": 2.0,
        "rr": 2.0
    },
    "FAST": {
        "label": "ACID FAST EXECUTION",
        "entry_tf": mt5.TIMEFRAME_M1,
        "trend_tf": mt5.TIMEFRAME_M5,
        "sl_atr": 1.2,
        "rr": 1.2
    }
}

lock = threading.RLock()
worker_thread = None
worker_stop = threading.Event()

state = {
    "connected": False,
    "armed": False,
    "running": False,
    "selected_bot": "SCALPER",
    "account": None,
    "last_error": "",
    "last_scan": None,
    "signals": [],
    "logs": [],
    "daily_trades": 0,
    "last_trade_time": {},
    "last_attempt_time": {}
}


# ============================================================
# LOGGING
# ============================================================

def log(message, level="INFO"):

    stamp = datetime.now().strftime("%H:%M:%S")

    with lock:
        state["logs"].insert(0, {
            "time": stamp,
            "level": level,
            "message": str(message)
        })

        state["logs"] = state["logs"][:100]


# ============================================================
# MT5 CONNECTION
# ============================================================

def mt5_connect():

    with lock:
        if (
            mt5.terminal_info() is not None
            and mt5.account_info() is not None
        ):
            state["connected"] = True
            return True

    ok = mt5.initialize(path=MT5_PATH)

    if not ok:
        ok = mt5.initialize()

    if not ok:

        error = mt5.last_error()

        with lock:
            state["connected"] = False
            state["last_error"] = str(error)

        return False

    with lock:
        state["connected"] = True
        state["last_error"] = ""

    return True


def account_snapshot():

    if not mt5_connect():
        return None

    account = mt5.account_info()
    terminal = mt5.terminal_info()

    if account is None:
        return None

    mode_map = {
        0: "DEMO",
        1: "CONTEST",
        2: "REAL"
    }

    return {
        "login": int(account.login),
        "server": str(account.server),
        "currency": str(account.currency),
        "balance": float(account.balance),
        "equity": float(account.equity),
        "profit": float(account.profit),
        "mode": mode_map.get(
            int(account.trade_mode), "UNKNOWN"
        ),
        "trade_allowed": bool(account.trade_allowed),
        "expert_allowed": bool(account.trade_expert),
        "terminal_trade_allowed": bool(
            getattr(terminal, "trade_allowed", False)
        ) if terminal else False
    }


def demo_gate():

    snapshot = account_snapshot()

    if not snapshot:
        return False, "MT5 disconnected."

    if snapshot["mode"] != "DEMO":
        return False, "BLOCKED: DEMO account required."

    if not all([
        snapshot["trade_allowed"],
        snapshot["expert_allowed"],
        snapshot["terminal_trade_allowed"]
    ]):
        return False, "MT5 trading permissions disabled."

    return True, "Verified DEMO account."


# ============================================================
# MARKET DATA
# ============================================================

def resolve_symbol(symbol):

    info = mt5.symbol_info(symbol)

    if info is None:
        return None

    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            return None

    return symbol


def rates(symbol, timeframe, count=100):

    data = mt5.copy_rates_from_pos(
        symbol, timeframe, 0, count
    )

    if data is None or len(data) < 60:
        return None

    return data


def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)
    result = float(values[0])

    for value in values[1:]:
        result = (
            float(value) * multiplier
            + result * (1.0 - multiplier)
        )

    return result


def atr_value(data, period=14):

    if len(data) < period + 2:
        return None

    ranges = []

    for i in range(1, len(data)):

        high = float(data[i]["high"])
        low = float(data[i]["low"])
        previous = float(data[i - 1]["close"])

        ranges.append(
            max(
                high - low,
                abs(high - previous),
                abs(low - previous)
            )
        )

    return sum(ranges[-period:]) / period


# ============================================================
# STRATEGY ENGINE
# ============================================================

def analyse(symbol, bot_key):

    config = BOTS[bot_key]

    entry = rates(symbol, config["entry_tf"])
    trend = rates(symbol, config["trend_tf"])

    if entry is None or trend is None:
        return {
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "Insufficient candle history"
        }

    entry = entry[:-1]
    trend = trend[:-1]

    closes = [float(x["close"]) for x in entry]
    trend_closes = [float(x["close"]) for x in trend]

    e20 = ema(closes[-70:], 20)
    e50 = ema(closes[-70:], 50)

    t20 = ema(trend_closes[-70:], 20)
    t50 = ema(trend_closes[-70:], 50)

    atr = atr_value(entry)

    if None in (e20, e50, t20, t50, atr) or atr <= 0:
        return {
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "Indicators not ready"
        }

    last = entry[-1]
    previous = entry[-2]

    close = float(last["close"])
    previous_close = float(previous["close"])

    trend_up = t20 > t50
    trend_down = t20 < t50

    signal = "WAIT"
    reason = "No confirmed setup"

    if bot_key == "SCALPER":

        if (
            trend_up
            and e20 > e50
            and close > float(previous["high"])
            and close > float(last["open"])
        ):
            signal = "BUY"
            reason = "M5 uptrend + M1 bullish breakout"

        elif (
            trend_down
            and e20 < e50
            and close < float(previous["low"])
            and close < float(last["open"])
        ):
            signal = "SELL"
            reason = "M5 downtrend + M1 bearish breakout"

    elif bot_key == "NORMAL":

        if (
            trend_up
            and close > e20
            and previous_close <= e20
        ):
            signal = "BUY"
            reason = "M15 trend + M5 EMA reclaim"

        elif (
            trend_down
            and close < e20
            and previous_close >= e20
        ):
            signal = "SELL"
            reason = "M15 trend + M5 EMA rejection"

    else:

        body = abs(close - float(last["open"]))

        if (
            trend_up
            and e20 > e50
            and close > e20
            and close > previous_close
            and body >= atr * 0.25
        ):
            signal = "BUY"
            reason = "M5 direction + M1 momentum"

        elif (
            trend_down
            and e20 < e50
            and close < e20
            and close < previous_close
            and body >= atr * 0.25
        ):
            signal = "SELL"
            reason = "M5 direction + M1 momentum"

    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)

    if tick is None or info is None:
        return {
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "No live tick"
        }

    return {
        "symbol": symbol,
        "signal": signal,
        "reason": reason,
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "atr": float(atr),
        "digits": int(info.digits),
        "time": datetime.now().strftime("%H:%M:%S")
    }


# ============================================================
# POSITION AND RISK MANAGEMENT
# ============================================================

def open_positions():

    positions = mt5.positions_get()

    if positions is None:
        return []

    return [
        p for p in positions
        if int(getattr(p, "magic", 0)) == MAGIC
    ]


def count_today_trades():

    start = datetime.combine(
        date.today(),
        datetime.min.time()
    )

    deals = mt5.history_deals_get(
        start, datetime.now()
    )

    if deals is None:
        return 0

    return sum(
        1 for deal in deals
        if (
            getattr(deal, "magic", 0) == MAGIC
            and getattr(deal, "entry", -1)
            == mt5.DEAL_ENTRY_IN
        )
    )


def calc_volume(
    symbol,
    order_type,
    entry_price,
    stop_price,
    balance
):

    info = mt5.symbol_info(symbol)

    if info is None:
        return 0.0, "Symbol information unavailable"

    risk_cash = balance * (
        RISK_PERCENT / 100.0
    )

    loss = mt5.order_calc_profit(
        order_type,
        symbol,
        1.0,
        entry_price,
        stop_price
    )

    if loss is None or abs(float(loss)) <= 0:
        return 0.0, "Could not calculate stop-loss risk"

    raw = risk_cash / abs(float(loss))

    step = float(info.volume_step or 0.01)
    minimum = float(info.volume_min or 0.01)
    maximum = float(info.volume_max or 100.0)

    volume = math.floor(raw / step) * step
    volume = min(volume, maximum)
    volume = round(volume, 8)

    if volume < minimum:
        return 0.0, (
            "Skipped: minimum broker lot exceeds "
            "the configured risk limit."
        )

    return volume, ""


# ============================================================
# BROKER-COMPATIBLE FILLING MODE
# ============================================================

def supported_filling_mode(info):

    execution = int(info.trade_exemode)
    flags = int(info.filling_mode)

    if execution == TRADE_EXECUTION_MARKET:

        if flags & SYMBOL_FILLING_FOK_FLAG:
            return mt5.ORDER_FILLING_FOK, "FOK"

        if flags & SYMBOL_FILLING_IOC_FLAG:
            return mt5.ORDER_FILLING_IOC, "IOC"

        return None, (
            "Broker reports no supported FOK or IOC policy. "
            f"Filling flags: {flags}"
        )

    return mt5.ORDER_FILLING_RETURN, "RETURN"


# ============================================================
# DEMO ORDER EXECUTION
# ============================================================

def execute_demo_order(signal):

    ok, message = demo_gate()

    if not ok:
        log(message, "BLOCK")
        return False

    if not state["armed"]:
        return False

    symbol = signal["symbol"]
    side = signal["signal"]

    if side not in ("BUY", "SELL"):
        return False

    now = time.time()

    if (
        now - state["last_trade_time"].get(symbol, 0)
        < COOLDOWN_SECONDS
    ):
        return False

    if (
        now - state["last_attempt_time"].get(symbol, 0)
        < REJECT_RETRY_SECONDS
    ):
        return False

    positions = open_positions()

    if any(p.symbol == symbol for p in positions):
        return False

    if len(positions) >= MAX_OPEN_POSITIONS:
        return False

    if state["daily_trades"] >= MAX_TRADES_PER_DAY:
        return False

    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if info is None or tick is None:
        log(f"{symbol}: missing market data", "WARN")
        return False

    is_buy = side == "BUY"

    order_type = (
        mt5.ORDER_TYPE_BUY
        if is_buy
        else mt5.ORDER_TYPE_SELL
    )

    price = float(tick.ask if is_buy else tick.bid)

    config = BOTS[state["selected_bot"]]

    distance = max(
        float(signal["atr"]) * config["sl_atr"],
        float(info.point) * 20
    )

    sl = (
        price - distance
        if is_buy
        else price + distance
    )

    tp = (
        price + distance * config["rr"]
        if is_buy
        else price - distance * config["rr"]
    )

    digits = int(info.digits)

    sl = round(sl, digits)
    tp = round(tp, digits)

    snapshot = account_snapshot()

    if not snapshot or snapshot["mode"] != "DEMO":

        state["armed"] = False

        log(
            "Account is not verified DEMO. Execution disarmed.",
            "BLOCK"
        )

        return False

    volume, reason = calc_volume(
        symbol,
        order_type,
        price,
        sl,
        snapshot["balance"]
    )

    if volume <= 0:

        log(f"{symbol}: {reason}", "BLOCK")

        state["last_attempt_time"][symbol] = now

        return False

    filling, filling_name = supported_filling_mode(info)

    if filling is None:

        log(
            f"{symbol}: {filling_name}. Order skipped.",
            "BLOCK"
        )

        state["last_attempt_time"][symbol] = now

        return False

    request_data = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling
    }

    state["last_attempt_time"][symbol] = now

    check = mt5.order_check(request_data)

    if check is None:

        log(
            f"{symbol}: order_check failed: {mt5.last_error()}",
            "ERROR"
        )

        return False

    if check.retcode not in (0, mt5.TRADE_RETCODE_DONE):

        log(
            f"{symbol}: pre-check rejected "
            f"({check.retcode}): {check.comment} "
            f"| filling={filling_name}",
            "BLOCK"
        )

        return False

    ok, message = demo_gate()

    if not ok or not state["armed"]:

        state["armed"] = False

        log(
            "Execution cancelled: safety gate changed.",
            "BLOCK"
        )

        return False

    result = mt5.order_send(request_data)

    if result is None:

        log(
            f"{symbol}: order_send failed: {mt5.last_error()}",
            "ERROR"
        )

        return False

    if result.retcode == mt5.TRADE_RETCODE_DONE:

        state["last_trade_time"][symbol] = now
        state["daily_trades"] += 1

        log(
            f"DEMO {side} {symbol} | "
            f"{volume} lots | {filling_name} | "
            f"SL {sl} | TP {tp} | "
            f"ticket {result.order}",
            "TRADE"
        )

        return True

    log(
        f"{symbol}: order failed "
        f"({result.retcode}): {result.comment}",
        "ERROR"
    )

    return False


# ============================================================
# SCANNER WORKER
# ============================================================

def worker():

    log("ACID FX PRO scanner started.")

    while not worker_stop.is_set():

        try:

            if not mt5_connect():
                time.sleep(3)
                continue

            selected = state["selected_bot"]
            results = []

            for symbol in SYMBOLS:

                if worker_stop.is_set():
                    break

                actual = resolve_symbol(symbol)

                if not actual:

                    results.append({
                        "symbol": symbol,
                        "signal": "WAIT",
                        "reason": "Symbol not found"
                    })

                    continue

                result = analyse(actual, selected)
                results.append(result)

                if (
                    result.get("signal") in ("BUY", "SELL")
                    and state["armed"]
                ):
                    execute_demo_order(result)

            with lock:

                state["signals"] = results

                state["last_scan"] = (
                    datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

                state["daily_trades"] = count_today_trades()

            time.sleep(2 if selected == "FAST" else 4)

        except Exception as exc:

            log(f"Worker error: {exc}", "ERROR")
            time.sleep(3)

    with lock:
        state["running"] = False

    log("Scanner stopped.")


# ============================================================
# WEBSITE ROUTES
# ============================================================

@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/api/status")
def api_status():

    snapshot = account_snapshot()

    with lock:

        state["account"] = snapshot

        state["daily_trades"] = (
            count_today_trades()
            if snapshot else 0
        )

        return jsonify({
            "connected": bool(snapshot),
            "account": snapshot,
            "armed": state["armed"],
            "running": state["running"],
            "selected_bot": state["selected_bot"],
            "signals": state["signals"],
            "logs": state["logs"][:25],
            "daily_trades": state["daily_trades"],
            "max_trades": MAX_TRADES_PER_DAY,
            "last_scan": state["last_scan"],
            "last_error": state["last_error"],
            "risk_percent": RISK_PERCENT,
            "max_positions": MAX_OPEN_POSITIONS
        })


@app.route("/api/select", methods=["POST"])
def api_select():

    key = (request.json or {}).get("bot", "SCALPER")

    if key not in BOTS:
        return jsonify({
            "ok": False,
            "message": "Unknown bot"
        }), 400

    if state["running"]:
        return jsonify({
            "ok": False,
            "message": "Stop scanner before changing bots."
        }), 409

    state["selected_bot"] = key

    log(f"Selected {BOTS[key]['label']}.")

    return jsonify({
        "ok": True,
        "bot": key
    })


@app.route("/api/arm", methods=["POST"])
def api_arm():

    ok, message = demo_gate()

    if not ok:

        state["armed"] = False

        return jsonify({
            "ok": False,
            "message": message
        }), 403

    if (request.json or {}).get("confirm_demo") is not True:

        return jsonify({
            "ok": False,
            "message": "Demo confirmation required."
        }), 400

    state["armed"] = True

    log("DEMO execution armed.", "WARN")

    return jsonify({
        "ok": True,
        "message": message
    })


@app.route("/api/disarm", methods=["POST"])
def api_disarm():

    state["armed"] = False

    log(
        "Execution disarmed. Existing positions remain open.",
        "WARN"
    )

    return jsonify({
        "ok": True,
        "message": "Execution disarmed."
    })


@app.route("/api/start", methods=["POST"])
def api_start():

    global worker_thread

    ok, message = demo_gate()

    if not ok:
        return jsonify({
            "ok": False,
            "message": message
        }), 403

    if not state["armed"]:
        return jsonify({
            "ok": False,
            "message": "Arm DEMO execution first."
        }), 403

    if state["running"]:
        return jsonify({
            "ok": True,
            "message": "Scanner already running."
        })

    worker_stop.clear()
    state["running"] = True

    worker_thread = threading.Thread(
        target=worker,
        daemon=True
    )

    worker_thread.start()

    return jsonify({
        "ok": True,
        "message": "DEMO scanner started."
    })


@app.route("/api/stop", methods=["POST"])
def api_stop():

    worker_stop.set()
    state["armed"] = False
    state["running"] = False

    log(
        "Emergency stop: execution disarmed. "
        "Existing positions remain open.",
        "WARN"
    )

    return jsonify({
        "ok": True,
        "message": "Execution disarmed and scanner stopping."
    })


@app.route("/api/positions")
def api_positions():

    if not mt5_connect():
        return jsonify({"positions": []})

    rows = []

    for position in open_positions():

        rows.append({
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "side": (
                "BUY"
                if position.type == mt5.POSITION_TYPE_BUY
                else "SELL"
            ),
            "volume": float(position.volume),
            "price": float(position.price_open),
            "sl": float(position.sl),
            "tp": float(position.tp),
            "profit": float(position.profit)
        })

    return jsonify({"positions": rows})


@app.route("/api/health")
def api_health():

    return jsonify({
        "ok": True,
        "service": "ACID FX PRO",
        "execution": "DEMO_ONLY",
        "magic": MAGIC,
        "authenticated": True
    })


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print("=" * 55)
    print("ACID FX PRO — PRIVATE DEMO EXECUTION ENGINE")
    print("=" * 55)
    print("Real accounts are blocked.")
    print("Execution starts DISARMED.")
    print("API authentication is ENABLED.")
    print()
    print("PRIVATE API TOKEN — enter this in your dashboard:")
    print(API_TOKEN)
    print()
    print("Token file:", TOKEN_FILE)
    print("Local server:", f"http://{HOST}:{PORT}")
    print("=" * 55)

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True
    )
