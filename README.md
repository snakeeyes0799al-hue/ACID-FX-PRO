# ACID FX PRO

**Automated MetaTrader 5 Trading Dashboard & Execution System**

ACID FX PRO is a Python-powered trading project designed to connect a web-based dashboard with MetaTrader 5 (MT5) for automated trading operations.

## Features

- Web-based trading dashboard
- MetaTrader 5 connectivity
- Automated BUY and SELL execution
- Multiple trading strategies
- Real-time account and trading status
- Stop and start controls
- Emergency disarm functionality
- Stop-loss and take-profit management
- Multi-symbol monitoring

## Trading Modes

### SCALPER
- M1 entry timeframe
- M5 trend confirmation
- Designed for short-term trading.

### NORMAL
- M5 entry timeframe
- M15 trend confirmation
- Designed for a more measured trading approach.

### FAST EXECUTION
- Prioritizes rapid execution of validated trading signals.

## Supported Markets

- XAUUSD
- EURUSD
- GBPUSD
- USDJPY

*Actual symbol availability depends on the connected broker.*

## Technology Stack

- Python
- Flask
- MetaTrader 5 Python API
- HTML
- CSS
- JavaScript

## Project Structure

```text
ACID-FX-PRO/
│
├── app.py
├── index.html
└── README.md
```

## Local Setup

1. Install MetaTrader 5 on your Windows computer.
2. Log in to an
