# XAUUSDT Telegram Alert Bot

Free GitHub Actions prototype for checking XAUUSDT futures and sending a Telegram alert when the setup exists.

It checks:

- 4H bullish trend above EMA 200
- 4H higher-low structure
- 1H support zone
- 1M liquidity sweep and reclaim
- Risk is within your configured max loss

The bot only sends alerts. It does not place trades.

Market data uses Bybit first. If Bybit blocks GitHub Actions with `403 Forbidden`, the bot falls back to Gate.io `XAU_USDT` public futures candles.

## GitHub Secrets

Add these repository secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Path:

```text
Repository > Settings > Secrets and variables > Actions > New repository secret
```

Never commit your Telegram bot token into the repo.

## Run Manually

In GitHub:

```text
Actions > XAU Telegram Alert > Run workflow
```

## Schedule

The workflow runs every 5 minutes:

```text
*/5 * * * *
```

GitHub Actions is free and easy, but it is not true 1-minute hosting.

## Current Risk Settings

Configured in `.github/workflows/xau-telegram-alert.yml`:

```text
ACCOUNT_BALANCE=50
MARGIN_USDT=5
LEVERAGE=10
MAX_RISK_USDT=0.50
MIN_RR=1.5
COOLDOWN_MINUTES=30
```

Start with low leverage while testing.
