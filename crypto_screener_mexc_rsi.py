# crypto_screener_mexc_rsi.py
import time
import requests
from datetime import datetime
import pandas as pd
import os
import json
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = Path("state.json")
TOP_N = 150
RSI_PERIOD = 14
RSI_OVERBOUGHT = 80.0
KLINES_LIMIT = 50

ALERT_TEMPLATE = """
Symbol: {symbol}
4H RSI(14): {rsi:.2f}
24h Vol: ${volume_usdt:,.0f} USDT
Timestamp: {now}
Link: https://www.mexc.co/futures/{symbol}
"""

# Setup retry session
session = requests.Session()
retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

# ──────────────────────────────────────────────
# STATE MANAGEMENT (file-based)
# ──────────────────────────────────────────────
def load_state() -> dict | None:
    if not STATE_FILE.exists():
        print("No state file found → treating as reset")
        return None
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        print(f"Loaded state: last reset {datetime.fromtimestamp(data['last_reset_time'])} UTC, "
              f"{len(data.get('alerted_symbols', []))} symbols")
        return data
    except Exception as e:
        print(f"State load failed: {e} → treating as reset")
        return None

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print("State saved to state.json")
    except Exception as e:
        print(f"State save failed: {e}")

# ──────────────────────────────────────────────
# RSI CALCULATION
# ──────────────────────────────────────────────
def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(com=period-1, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ──────────────────────────────────────────────
# TELEGRAM SEND
# ──────────────────────────────────────────────
def send_telegram(msg: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_notification": False,
    }
    try:
        r = session.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False

# ──────────────────────────────────────────────
# GET TOP TICKERS + KLINES
# ──────────────────────────────────────────────
def get_futures_tickers() -> list[dict]:
    url = "https://contract.mexc.com/api/v1/contract/ticker"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        tickers = data.get("data", [])
        if isinstance(tickers, dict):
            tickers = [tickers]
        usdt_perps = [
            t for t in tickers
            if isinstance(t, dict) and str(t.get("symbol", "")).endswith("_USDT")
        ]
        usdt_perps.sort(key=lambda x: float(x.get("amount24", 0)), reverse=True)
        return usdt_perps[:TOP_N]
    except Exception as e:
        print(f"Error fetching tickers: {e}")
        return []

def get_4h_klines(symbol: str) -> pd.DataFrame | None:
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
    params = {"interval": "Hour4", "limit": KLINES_LIMIT}
    try:
        r = session.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("success") or not data.get("data"):
            print(f"No kline data for {symbol}")
            return None
        klines = data["data"]
        df = pd.DataFrame({
            "open_time": klines["time"],
            "open": klines["open"],
            "high": klines["high"],
            "low": klines["low"],
            "close": klines["close"],
            "vol": klines["vol"],
            "amount": klines["amount"],
        })
        df["close"] = df["close"].astype(float)
        df = df.sort_values("open_time").reset_index(drop=True)
        if len(df) < RSI_PERIOD + 10:
            print(f"Too few candles for {symbol} ({len(df)})")
            return None
        return df
    except Exception as e:
        print(f"Klines failed for {symbol}: {e}")
        return None

# ──────────────────────────────────────────────
# MAIN SCAN
# ──────────────────────────────────────────────
def run_scan():
    now = datetime.utcnow()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S UTC')
    current_hour = now.hour
    is_reset = (current_hour % 4 == 0)
    print(f"[{now_str}] Starting MEXC 4h RSI > {RSI_OVERBOUGHT} scan... (Reset: {is_reset})")

    state = load_state()
    if state is None:
        is_reset = True

    tickers = get_futures_tickers()
    if not tickers:
        print("No tickers received → exiting")
        return

    print(f"Scanning top {len(tickers)} USDT perpetual pairs...")
    hits = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if not symbol:
            continue
        df = get_4h_klines(symbol)
        if df is None:
            time.sleep(0.7)
            continue
        rsi_series = calculate_rsi(df["close"], RSI_PERIOD)
        latest_rsi = rsi_series.iloc[-1]
        if pd.isna(latest_rsi):
            time.sleep(0.7)
            continue
        if latest_rsi > RSI_OVERBOUGHT:
            volume_usdt = float(ticker.get("amount24", 0))
            alert_data = {
                "symbol": symbol,
                "symbol_clean": symbol.replace("_", ""),
                "rsi": latest_rsi,
                "volume_usdt": volume_usdt
            }
            hits.append(alert_data)
        time.sleep(0.9)

    if not hits:
        print("No pairs with 4h RSI > 80 found.")
        if is_reset:
            save_state({"last_reset_time": now.timestamp(), "alerted_symbols": []})
        return

    print(f"Found {len(hits)} overbought pair(s)")

    alert_hits = []
    if is_reset:
        alert_hits = hits
    else:
        previous_symbols = set(state.get("alerted_symbols", []))
        alert_hits = [hit for hit in hits if hit["symbol"] not in previous_symbols]

    if alert_hits:
        for hit in alert_hits:
            message = ALERT_TEMPLATE.format(
                now=now.strftime("%Y-%m-%d %H:%M UTC"),
                **hit
            )
            success = send_telegram(message)
            status = "OK" if success else "FAILED"
            print(f"→ {hit['symbol']} RSI {hit['rsi']:.1f} → {status}")
            time.sleep(1.6)
    else:
        print("No new alerts to send this hour.")

    # Only save/update state on reset hours
    if is_reset:
        alerted_symbols = [hit["symbol"] for hit in hits]
        save_state({
            "last_reset_time": now.timestamp(),
            "alerted_symbols": alerted_symbols
        })

if __name__ == "__main__":
    run_scan()
