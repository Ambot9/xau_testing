#!/usr/bin/env python3
"""
Free XAUUSDT multi-timeframe Telegram alert bot.

It checks:
- 4H trend bias with EMA 200
- 1H support zone
- 1M liquidity sweep + reclaim trigger
- account risk based on balance, margin, leverage, and stop distance

No exchange API key is needed because this only reads public candles.
It does not place trades.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
GATE_FUTURES_CANDLE_URL = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
BYBIT_INTERVALS = {"1m": "1", "1h": "60", "4h": "240"}


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Config:
    symbol: str
    gate_contract: str
    category: str
    check_seconds: int
    account_balance: float
    margin_usdt: float
    leverage: float
    max_risk_usdt: float
    min_rr: float
    zone_buffer_pct: float
    telegram_token: str
    telegram_chat_id: str
    state_file: Path
    cooldown_minutes: int


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value in (None, "") else float(value)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value in (None, "") else int(value)


def load_config() -> Config:
    return Config(
        symbol=os.getenv("SYMBOL", "XAUUSDT").upper(),
        gate_contract=os.getenv("GATE_CONTRACT", "XAU_USDT").upper(),
        category=os.getenv("BYBIT_CATEGORY", "linear"),
        check_seconds=env_int("CHECK_SECONDS", 60),
        account_balance=env_float("ACCOUNT_BALANCE", 50.0),
        margin_usdt=env_float("MARGIN_USDT", 5.0),
        leverage=env_float("LEVERAGE", 10.0),
        max_risk_usdt=env_float("MAX_RISK_USDT", 0.50),
        min_rr=env_float("MIN_RR", 1.5),
        zone_buffer_pct=env_float("ZONE_BUFFER_PCT", 0.0012),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        state_file=Path(os.getenv("STATE_FILE", "work/xau_alert_state.json")),
        cooldown_minutes=env_int("COOLDOWN_MINUTES", 30),
    )


def http_get_json(url: str, params: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "xau-alert-bot/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bybit_klines(config: Config, interval: str, limit: int) -> list[Candle]:
    payload = http_get_json(
        BYBIT_KLINE_URL,
        {
            "category": config.category,
            "symbol": config.symbol,
            "interval": interval,
            "limit": limit,
        },
    )
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {payload.get('retMsg', payload)}")

    rows = payload["result"]["list"]
    candles = [
        Candle(
            start_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
    ]
    return sorted(candles, key=lambda candle: candle.start_ms)


def fetch_gate_klines(config: Config, interval: str, limit: int) -> list[Candle]:
    rows = http_get_json(
        GATE_FUTURES_CANDLE_URL,
        {
            "contract": config.gate_contract,
            "interval": interval,
            "limit": limit,
        },
    )
    candles = [
        Candle(
            start_ms=int(row["t"]) * 1000,
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
            volume=float(row["v"]),
        )
        for row in rows
    ]
    return sorted(candles, key=lambda candle: candle.start_ms)


def fetch_klines(config: Config, interval: str, limit: int) -> list[Candle]:
    bybit_interval = BYBIT_INTERVALS.get(interval, interval)
    try:
        return fetch_bybit_klines(config, bybit_interval, limit)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
        print(f"Bybit returned 403 for {config.symbol}; using Gate.io {config.gate_contract} fallback", file=sys.stderr)
    except urllib.error.URLError as error:
        print(f"Bybit unavailable for {config.symbol}: {error}; using Gate.io {config.gate_contract} fallback", file=sys.stderr)
    return fetch_gate_klines(config, interval, limit)


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values for EMA {period}")
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        raise ValueError(f"Need at least {period + 1} candles for ATR")
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges[-period:]) / period


def recent_swing_lows(candles: list[Candle], lookback: int = 80) -> list[float]:
    sample = candles[-lookback:]
    lows: list[float] = []
    for index in range(2, len(sample) - 2):
        current = sample[index]
        if (
            current.low < sample[index - 1].low
            and current.low < sample[index - 2].low
            and current.low < sample[index + 1].low
            and current.low < sample[index + 2].low
        ):
            lows.append(current.low)
    return lows


def find_support_zone(candles_1h: list[Candle], buffer_pct: float) -> tuple[float, float, float]:
    swing_lows = recent_swing_lows(candles_1h)
    reference_low = swing_lows[-1] if swing_lows else min(candle.low for candle in candles_1h[-30:])
    buffer = reference_low * buffer_pct
    return reference_low - buffer, reference_low + buffer, reference_low


def higher_low_formed(candles_4h: list[Candle]) -> bool:
    lows = recent_swing_lows(candles_4h, lookback=120)
    return len(lows) >= 2 and lows[-1] > lows[-2]


def analyze(config: Config, candles_4h: list[Candle], candles_1h: list[Candle], candles_1m: list[Candle]) -> dict[str, Any]:
    closes_4h = [candle.close for candle in candles_4h]
    ema_200 = ema(closes_4h, 200)
    last_4h = candles_4h[-1]
    bullish_4h = last_4h.close > ema_200
    has_higher_low = higher_low_formed(candles_4h)

    zone_low, zone_high, support = find_support_zone(candles_1h, config.zone_buffer_pct)
    recent_1m = candles_1m[-12:]
    latest = candles_1m[-1]

    swept_below_support = any(candle.low < support for candle in recent_1m[:-1])
    reclaimed_zone = latest.close > zone_high
    bullish_confirmation = latest.close > latest.open
    near_zone = min(candle.low for candle in recent_1m) <= zone_high

    entry = latest.close
    stop = min(candle.low for candle in recent_1m) - atr(candles_1m, 14) * 0.20
    risk_distance = max(entry - stop, 0.0)
    tp1 = entry + risk_distance
    tp2 = entry + risk_distance * 2
    rr = (tp2 - entry) / risk_distance if risk_distance > 0 else 0

    position_notional = config.margin_usdt * config.leverage
    risk_pct_move = risk_distance / entry if entry > 0 else math.inf
    risk_usdt = position_notional * risk_pct_move

    reasons = []
    if bullish_4h:
        reasons.append(f"4H close is above EMA200 ({ema_200:.2f})")
    if has_higher_low:
        reasons.append("4H higher low structure detected")
    if near_zone:
        reasons.append(f"1H support zone touched near {zone_low:.2f}-{zone_high:.2f}")
    if swept_below_support and reclaimed_zone:
        reasons.append("1M swept below support and reclaimed the zone")

    allowed = all(
        [
            bullish_4h,
            has_higher_low,
            near_zone,
            swept_below_support,
            reclaimed_zone,
            bullish_confirmation,
            risk_distance > 0,
            rr >= config.min_rr,
            risk_usdt <= config.max_risk_usdt,
        ]
    )

    return {
        "allowed": allowed,
        "bias": "BUY" if allowed else "NO TRADE",
        "symbol": config.symbol,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "risk_usdt": risk_usdt,
        "risk_distance": risk_distance,
        "position_notional": position_notional,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "ema_200": ema_200,
        "latest_close": latest.close,
        "invalid_if": zone_low,
        "reasons": reasons,
        "checks": {
            "bullish_4h": bullish_4h,
            "higher_low_4h": has_higher_low,
            "near_1h_zone": near_zone,
            "swept_below_support_1m": swept_below_support,
            "reclaimed_zone_1m": reclaimed_zone,
            "bullish_confirmation_1m": bullish_confirmation,
            "risk_ok": risk_usdt <= config.max_risk_usdt,
        },
    }


def format_signal(signal: dict[str, Any], config: Config) -> str:
    checks = signal["checks"]
    status = "BUY SETUP FOUND" if signal["allowed"] else "NO TRADE"
    reason_text = "\n".join(f"- {reason}" for reason in signal["reasons"]) or "- Conditions not aligned"
    check_text = "\n".join(f"- {name}: {'yes' if value else 'no'}" for name, value in checks.items())
    return (
        f"{status}\n"
        f"Pair: {signal['symbol']} Futures\n"
        f"Bias: {signal['bias']}\n"
        f"4H EMA200: {signal['ema_200']:.2f}\n"
        f"1H Zone: {signal['zone_low']:.2f}-{signal['zone_high']:.2f}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"Stop Loss: {signal['stop']:.2f}\n"
        f"TP1: {signal['tp1']:.2f}\n"
        f"TP2: {signal['tp2']:.2f}\n"
        f"Risk/Reward: 1:{signal['rr']:.2f}\n"
        f"Risk: ${signal['risk_usdt']:.2f} / max ${config.max_risk_usdt:.2f}\n"
        f"Margin: ${config.margin_usdt:.2f} at {config.leverage:g}x = ${signal['position_notional']:.2f} position\n"
        f"Invalid If: 1M candle closes below {signal['invalid_if']:.2f}\n\n"
        f"Reasons:\n{reason_text}\n\n"
        f"Checks:\n{check_text}\n\n"
        "Reminder: this is an alert, not guaranteed profit. Use stop loss."
    )


def format_startup_message(config: Config) -> str:
    return (
        "XAU alert bot started\n"
        f"Pair: {config.symbol} Futures\n"
        f"Fallback Data: Gate.io {config.gate_contract}\n"
        f"Margin: ${config.margin_usdt:.2f}\n"
        f"Leverage: {config.leverage:g}x\n"
        f"Max Risk: ${config.max_risk_usdt:.2f}\n"
        "Status: checking market conditions now"
    )


def send_telegram(config: Config, text: str) -> None:
    if not config.telegram_token or not config.telegram_chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": config.telegram_chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        TELEGRAM_SEND_URL.format(token=config.telegram_token),
        data=data,
        headers={"User-Agent": "xau-alert-bot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram error: {payload}")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def should_alert(config: Config, signal: dict[str, Any], now: int) -> bool:
    if not signal["allowed"]:
        return False
    state = load_state(config.state_file)
    last_alert_at = int(state.get("last_alert_at", 0))
    cooldown_seconds = config.cooldown_minutes * 60
    return now - last_alert_at >= cooldown_seconds


def mark_alerted(config: Config, signal: dict[str, Any], now: int) -> None:
    save_state(
        config.state_file,
        {
            "last_alert_at": now,
            "symbol": signal["symbol"],
            "entry": signal["entry"],
            "stop": signal["stop"],
            "tp2": signal["tp2"],
        },
    )


def run_once(config: Config, dry_run: bool = False, send_no_trade: bool = False) -> dict[str, Any]:
    candles_4h = fetch_klines(config, "4h", 240)
    candles_1h = fetch_klines(config, "1h", 160)
    candles_1m = fetch_klines(config, "1m", 120)
    signal = analyze(config, candles_4h, candles_1h, candles_1m)
    message = format_signal(signal, config)

    now = int(time.time())
    if dry_run:
        print(message)
        return signal

    if should_alert(config, signal, now):
        send_telegram(config, message)
        mark_alerted(config, signal, now)
        print(f"Sent BUY alert for {config.symbol}")
    elif send_no_trade:
        send_telegram(config, message)
        print(f"Sent status message for {config.symbol}")
    else:
        print(f"{signal['bias']}: conditions checked, no Telegram alert sent")

    return signal


def self_test() -> None:
    candles_4h = [Candle(i, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100.5 + i * 0.1, 1) for i in range(240)]
    candles_1h = [Candle(i, 120, 121, 119 + i * 0.01, 120, 1) for i in range(160)]
    candles_1m = [Candle(i, 120, 120.5, 119.7, 120.1, 1) for i in range(120)]
    config = load_config()
    signal = analyze(config, candles_4h, candles_1h, candles_1m)
    assert "allowed" in signal
    assert signal["entry"] > 0
    print("Self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="XAUUSDT Telegram alert bot")
    parser.add_argument("--once", action="store_true", help="Check one time and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print message instead of sending Telegram")
    parser.add_argument("--send-startup", action="store_true", help="Send a Telegram startup message before checking")
    parser.add_argument("--send-no-trade", action="store_true", help="Also send Telegram when conditions are not aligned")
    parser.add_argument("--self-test", action="store_true", help="Run offline logic test")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    config = load_config()
    if args.send_startup:
        if args.dry_run:
            print(format_startup_message(config))
        else:
            send_telegram(config, format_startup_message(config))

    if args.once:
        run_once(config, dry_run=args.dry_run, send_no_trade=args.send_no_trade)
        return 0

    while True:
        try:
            run_once(config, dry_run=args.dry_run, send_no_trade=args.send_no_trade)
        except Exception as error:
            print(f"Error: {error}", file=sys.stderr)
        time.sleep(config.check_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
