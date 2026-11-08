# Price Tracker Bot

Telegram bot: users paste a product link, the bot watches the price and messages them when it drops.
Free users get 2 links, Premium gets more and faster checks, and the owner is unlimited.

## Run it (Python 3.10+)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env          # Linux/Mac: cp .env.example .env
# edit .env: set BOT_TOKEN (from @BotFather) and OWNER_IDS (your Telegram id from @userinfobot)

python -m unittest discover -s tests -t .     # should print OK
python main.py
```

## What the bot does

| Command | Who | What |
|---|---|---|
| *(paste a link)* or `/add <link>` | everyone | start tracking (up to 5 links per message) |
| `/list` | everyone | tracked products with ❌ remove buttons |
| `/target <id> <price>` / `/target <id> off` | everyone | alert only at or below that price |
| `/remove <id>` | everyone | stop tracking |
| `/plan` | everyone | plan, slots used, check speed |
| `/premium` | everyone | pay with Telegram Stars |
| `/grant <user_id> <days>` | owner | give premium manually (UPI/cash sales) |
| `/stats` | owner | users, tracks, Stars earned |
| `/monitor` | owner | live price-monitor summary (checks, failures, last/next check, average fetch time) |

## How checking works

* Every **unique product is fetched once**, however many people track it.
* Check speed per product = the fastest plan among its trackers
  (free 30 min, premium 5 min, owner 15 s; all configurable in `.env`).
* At most one request every `HOST_MIN_GAP` seconds per website, so many owner links on one
  site queue up politely instead of getting your IP banned.
* One failed check keeps the normal cadence; repeated failures back off (x2 each time, up to 6 h);
  a blocked store (403/429/captcha) gets a longer cool-down (see below). After 8 failures, or a 404,
  the product is paused and its trackers are told. Sending the link again resumes it.
* Alert rules: any drop of 0.5% or more, or, if a target is set, when the price reaches the target.

## Price-check logs and monitoring

Every real check attempt is logged, so you can verify the 15-second interval yourself.
Console and `logs/price_checks.log` (rotating, 5 MB x 5) get one line per attempt:

```
[16:00:15] Amazon | ₹92,990 | SUCCESS | #12 interval=15s actual=15.0s late=+0.0s wait=0.0s fetch=0.84s http=200 prev=₹92,990 change=±₹0 trackers=1 url=https://www.amazon.in/dp/B09B8V1LZ3
[16:00:30] Amazon | ₹92,490 | PRICE DROP ₹500 (-0.5%) | #12 interval=15s actual=15.0s ...
[16:00:45] Amazon | BLOCKED | HTTP 403 blocked by the store | #12 ... http=403 retry_in=300s
```

| Field | Meaning |
|---|---|
| `interval` | scheduled interval for this product (fastest plan among its trackers) |
| `actual` | real time since this product's previous attempt started (`first` after a restart) |
| `late` | how long after its scheduled time the check really started |
| `wait` | time spent waiting for the per-website gap / a free fetch slot |
| `fetch` / `http` | fetch duration and HTTP status code |
| `prev` / `change` | previously recorded price and the difference |
| `url` | scheme + host + path only (query strings can hold secrets) |

The same data is stored in the `check_log` table (kept `CHECK_LOG_HOURS`, default 24 h; price
history is never deleted). At startup the monitor prints its three intervals, and every
`SUMMARY_SECONDS` it prints a summary: successful / failed checks, last successful check, next
scheduled check and average fetch duration. The owner can ask for the same summary any time with `/monitor`.

**How the schedule stays accurate:** the next check is anchored to the scheduled time, not to when
the fetch finished, so a slow page does not push later checks back. After a restart or outage the
monitor re-anchors instead of firing a burst of catch-up checks. A product is never fetched twice at once.

**Reading the log honestly:** `OWNER_INTERVAL=15` is what the bot *asks* for. Whether the store allows it
is shown by `http=` and `BLOCKED` lines. Also, `HOST_MIN_GAP` (default 2 s) spaces requests to one website,
so more than about 7 owner products on the same store cannot all fit in 15 s; their `late=` values will grow.

**Blocks are respected:** HTTP 403, HTTP 429 and captcha pages are logged as `BLOCKED`. The bot does not retry
with other identities or try to get past the protection. It leaves every product of that store alone for
5 minutes (doubling on each repeat, up to 6 h, or the store's `Retry-After`), then tries again normally.

Secrets are masked in all logs: `BOT_TOKEN`, proxy passwords and anything that looks like a Telegram token.

## Making money

1. **Affiliate links (main income).** Every link the bot shows (alerts and `/list`) goes through
   `bot/affiliate.py`. Fill `AMAZON_TAG` / `FLIPKART_AFFID`, or use `AFFILIATE_TEMPLATE` for
   EarnKaro/Cuelinks-style wrappers. Leave them empty and links pass through unchanged.
2. **Premium via Telegram Stars.** `/premium` sends a Stars invoice (`PREMIUM_STARS`, `PREMIUM_DAYS`).
   Payments are stored once per charge id, so a duplicate delivery can't grant twice.
3. **Later:** a public deals channel that the bot posts big drops to, with your affiliate links.

Check each affiliate program's rules before launching. Amazon Associates in particular
requires disclosure and has its own product API.

## Project layout

```
main.py              entry point
bot/app.py           builds everything and runs (composition root)
bot/handlers.py      Telegram commands (thin)
bot/service.py       plans, limits, add-link flow (no Telegram/HTTP inside)
bot/scheduler.py     background price monitor (drift-free schedule, block handling)
bot/checklog.py      per-check log line, startup banner, monitor statistics
bot/logging_setup.py console + rotating log files, secret redaction
bot/fetcher.py       HTTP: SSRF checks, size cap, redirects, proxies, UA rotation
bot/parsers.py       HTML -> title + price (JSON-LD, meta, Amazon, generic)
bot/urlsafety.py     link validation and private-network blocking
bot/alerts.py        when to alert + message/price formatting
bot/affiliate.py     affiliate link building
bot/database.py      SQLite storage
tests/               unit tests (fake fetcher, no network needed)
```

## Known limits

* Scraping is fragile. Sites change their pages and some block bots, especially Amazon at high
  volume. If many checks fail, add proxies in `PROXY_URLS` or use the store's official
  API/product feed. Tell me which stores matter most and I'll add dedicated parsers.
* SQLite and a single process are fine for thousands of users. Move to Postgres only if you outgrow it.
* The bot must run 24/7 to monitor, so use a cheap VPS rather than your PC.

## Render Web Service deployment (Telegram long polling)

This project can expose a lightweight HTTP health endpoint while continuing to use Telegram
long polling. The server binds to `0.0.0.0:$PORT` (Render supplies `PORT`; local fallback is
`10000`). Render health-check paths `/health` and `/healthz` return HTTP 200 with
`{"status":"ok"}`. Keep the Start Command as `python main.py`.

**Important free-plan limits:** a Free Web Service can still spin down after inactivity, so
Telegram polling is not guaranteed to stay online 24/7. Its filesystem is ephemeral; the
SQLite database at `DB_PATH` can be lost on restart, redeploy or spin-down. Use persistent
managed database storage or an appropriate persistent disk/hosting plan before relying on
this for real users or payments. The health endpoint fixes the missing-port startup problem;
it does not remove Free-plan sleep or storage limits.


## Button-based target prices

Open `/list` and tap **🎯 Set Target** on a product. Choose a quick target 5%, 10%, or 15% below its current price, or tap **✍️ Enter my own price** and send the amount. Existing `/target` commands remain available for compatibility. A saved target can be changed or cleared from the same product controls.
