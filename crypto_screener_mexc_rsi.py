# crypto_screener_mexc_rsi.py
# Scans MEXC USDT perpetual futures for 4h RSI(14) > 80
# Sends alerts to your Telegram chat

import time
import requests
from datetime import datetime
import pandas as pd
import os

# ──────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# How many top pairs to scan (sorted by 24h volume)
TOP_N = 150

# RSI settings
RSI_PERIOD      = 14
RSI_OVERBOUGHT  = 80.0

# How many 4h candles to fetch (must be > RSI_PERIOD)
KLINES_LIMIT = 100

ALERT_TEMPLATE = """
Symbol:     {symbol}
4H RSI(14): {rsi:.2f}
24h Vol:    ${volume_usdt:,.0f} USDT
Timestamp:  {now}
Link:       https://futures.mexc.com/exchange/{symbol_clean}
"""

# ──────────────────────────────────────────────
#  RSI CALCULATION
# ──────────────────────────────────────────────

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    # Smoothed averages after first period
    for i in range(period, len(series)):
        avg_gain.iloc[i] = (avg_gain.iloc[i-1] * (period-1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i-1] * (period-1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def send_telegram(msg: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_notification": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        else:
            print(f"Telegram error {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def get_futures_tickers() -> list[dict]:
    """Get top USDT perpetual tickers sorted by volume"""
    url = "https://contract.mexc.com/api/v1/contract/ticker"
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return []

        tickers = data.get("data", [])
        if isinstance(tickers, dict):
            tickers = [tickers]

        # Filter USDT perpetuals only
        usdt_perps = [
            t for t in tickers
            if isinstance(t, dict) and str(t.get("symbol", "")).endswith("_USDT")
        ]

        # Sort by 24h volume (quote currency if available, else fallback)
        usdt_perps.sort(
            key=lambda x: float(x.get("amount24", x.get("vol24", 0))),
            reverse=True
        )

        return usdt_perps[:TOP_N]
    except Exception as e:
        print(f"Error fetching tickers: {e}")
        return []


def get_4h_klines(symbol: str) -> pd.DataFrame | None:
    url = "https://contract.mexc.com/api/v1/contract/kline/"
    params = {
        "symbol": symbol,
        "interval": "4h",
        "limit": KLINES_LIMIT,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("success") or not data.get("data"):
            return None

        klines = data["data"]
        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "vol", "amount"
        ])
        df["close"] = df["close"].astype(float)
        df = df.sort_values("open_time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Klines failed for {symbol}: {e}")
        return None


# ──────────────────────────────────────────────
#  MAIN SCAN
# ──────────────────────────────────────────────

def run_scan():
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"[{now_str}] Starting MEXC 4h RSI > {RSI_OVERBOUGHT} scan...")

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
        if df is None or len(df) < RSI_PERIOD + 10:
            time.sleep(0.7)
            continue

        rsi_series = calculate_rsi(df["close"], RSI_PERIOD)
        latest_rsi = rsi_series.iloc[-1]

        if pd.isna(latest_rsi):
            time.sleep(0.7)
            continue

        if latest_rsi > RSI_OVERBOUGHT:
            volume_usdt = float(ticker.get("amount24", 0))  # quote volume (USDT)
            if volume_usdt == 0:
                volume_usdt = float(ticker.get("vol24", 0)) * df["close"].iloc[-1]

            alert_data = {
                "symbol": symbol,
                "symbol_clean": symbol.replace("_", ""),
                "rsi": latest_rsi,
                "volume_usdt": volume_usdt
            }
            hits.append(alert_data)

        time.sleep(0.9)  # polite delay (~60 req/min)

    if not hits:
        print("No pairs with 4h RSI > 80 found.")
        return

    print(f"Found {len(hits)} overbought pair(s)")

    for hit in hits:
        message = ALERT_TEMPLATE.format(
            now=datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
            **hit
        )
        success = send_telegram(message)
        status = "OK" if success else "FAILED"
        print(f"→ {hit['symbol']}  RSI {hit['rsi']:.1f}  → {status}")
        time.sleep(1.6)  # Telegram rate limit safety


if __name__ == "__main__":
    run_scan()
