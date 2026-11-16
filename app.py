
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
        "execution": state.get("execution_mode", "DEMO"),
        "supported_modes": ["DEMO", "LIVE"],
        "magic": MAGIC,
        "authenticated": True
    })


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print("=" * 55)
    print("ACID FX PRO — PRIVATE DEMO/LIVE EXECUTION ENGINE")
    print("=" * 55)
    print("Execution mode defaults to DEMO.")
    print("LIVE requires selecting LIVE, a REAL MT5 account, and explicit arming confirmation.")
    print("Execution starts DISARMED after every restart.")
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
