# Financial News Data Feed (RSS -> de-dup -> priority -> API / live stream / alerts)

Live dashboard at http://localhost:8000 (filters, priority badges, watchlist, CSV download).

## Run on your PC
Windows: double-click `run.bat`. Or manually:

    pip install -r requirements.txt
    python -m uvicorn app:app --host 0.0.0.0 --port 8000

API docs: http://localhost:8000/docs

## Priority and the watchlist
Every story gets a score and a level: **high**, **medium** or **low** (rules in `priority.py`).

| What raises the score | Points |
|---|---|
| Topic: earnings, M&A, rates & policy (15), IPO / selloff (12), rally (10)... (max 25 in total) | up to 25 |
| A named company | +8 |
| "Breaking / just in / exclusive / alert" in the headline | +15 |
| Strong tone (bullish or bearish) | +5 |
| Each extra site carrying the same story | +10 (max 30) |
| **Mentions a company on your watchlist** | **+35** |

High = 60+, medium = 35+. Change the numbers at the top of `priority.py`.
The watchlist bonus is added when stories are *read*, so editing the watchlist re-ranks old stories at once.

- **Dashboard:** `Any priority / High / Medium+ / ★ Watchlist` buttons, `Newest / Top priority` sorting, and the `★ Watchlist` button (top) to add or remove companies.
- **API:** `GET /api/news?min_priority=high`, `?watchlist=true`, `?sort=priority`. The same filters work on `/api/export`, `/api/events` and `/api/stream`. Every story carries `priority: {score, level, watch}`.
- **Watchlist API:** `GET/PUT /api/watchlist`, `POST/DELETE /api/watchlist/{company}`, and `GET /api/companies` for the valid names. It is stored in `watchlist.json` (you can also edit that file by hand; no restart).
- A story's score can rise after it first appears (more sites pick it up). Alerts are sent once, when the story first arrives, so they use the score at that moment.

## Companies (about 290)
`companies.py` has the Nifty 50, Next 50 and many mid-caps, recent IPOs and large global names, each with aliases and an NSE ticker.
Add one line, then restart:

    ("Zydus Wellness", ["zydus wellness"], "ZYDUSWELL"),

Alias forms: plain text = whole-word match; `"=TCS"` = case-sensitive (for short tickers); `"re:..."` = regular expression
(used to avoid look-alikes, e.g. "India's reliance on imports" is not Reliance). Only stories fetched after the restart get new tags.

## Telegram alerts
1. In Telegram open **@BotFather** -> `/newbot` -> copy the token.
2. Open your new bot and send it "hi".
3. Run:

       python telegram_setup.py <token> high

   Modes: `high` (default), `medium`, `all`, `watchlist`. It finds your chat, writes `notify.json` and sends a confirmation message.

By hand: copy `notify.example.json` to `notify.json`. Alert filters now include
`"min_priority": "high"` or `"medium"` and `"watchlist_only": true`, next to the older `companies`, `sectors`, `trends`, `sentiment`, `keywords`, `events`.
You can define several Telegram channels, e.g. one for high priority and one for the watchlist.
Messages look like `★ HIGH [NEW] Moneycontrol: Reliance Industries Q2 profit jumps 18%...`. More than 8 at once are sent as one digest, most important first.
Test: `/docs` -> `POST /api/notify/test`. No restart is needed after editing `notify.json`.

## Run on a server, 24/7, with HTTPS (Docker)
You need a small Linux server (1 GB RAM is enough) with Docker, and a domain name whose DNS **A record points to the server**. Ports 80 and 443 must be open.

    git clone / copy this folder to the server, then:
    ./setup.sh

`setup.sh` asks for the domain, a dashboard username and password, and the website that may call the API; it writes `.env`, generates an API key and starts everything.

What you get:
- **Caddy** in front: automatic HTTPS certificate (Let's Encrypt), renewed by itself. The app itself is not reachable from the internet.
- **Login** (username + password) for the dashboard, `/docs` and every change (watchlist, refresh).
- **API key** for machines: your website, scripts, Excel and Google Sheets send header `X-API-Key: <key>` and can only read `GET /api/*`.
  Call it from your site's *server* code. If the key is placed in browser JavaScript, anyone can see it.
- **Data in `./data`**: `news.db`, `notify.json`, `watchlist.json`, `feeds.json` (all editable on the server; no restart needed except `companies.py`).
- **Daily backups** in `./data/backups` (last 7 kept). Copy that folder off the server now and then.
- Restarts itself after a reboot or crash, and a health check at `/api/health`.

Everyday commands:

    docker compose logs -f app        # what it is doing
    docker compose up -d --build      # after changing code (e.g. companies.py)
    docker compose down               # stop
    # restore a backup: stop, copy data/backups/news-YYYYMMDD.db over data/news.db, start

Telegram on the server: run `telegram_setup.py` on any computer, copy the `notify.json` it creates into `./data/`.
To test HTTPS on your own machine use `localhost` as the domain (your browser will warn about the local certificate).
Server settings (optional, in `.env`): `FETCH_INTERVAL`, `RETENTION_DAYS`, `BACKUP_KEEP`, `CORS_ORIGINS`.

## No duplicates
- Same article with different link spelling (http/https, www, utm tags, trailing slash) = one row.
- Same story from different sites (similar headline) = one row; the other sites are listed in `also_reported_by`.
- Opposite or different numbers are never merged ("Nifty ends higher" vs "ends lower", "+200 pts" vs "-150 pts").
- Tuning (environment variables): `DEDUP_THRESHOLD` (default 0.8, lower = merges more), `DEDUP_WINDOW_H` (default 24).

## Near real-time
Feeds are checked every 60 seconds (`FETCH_INTERVAL`). RSS is not instant: a story shows up within about a minute of the publisher adding it.
- `GET /api/stream` pushes each new/updated story the moment it is stored (Server-Sent Events). Try: `python listen.py`
- `GET /api/events?after=0` is the polling version; pass back `last_seq` each time.

## Endpoints
`/api/news`, `/api/export`, `/api/meta`, `/api/refresh`, `/api/stream`, `/api/events`, `/api/notify/test`,
`/api/watchlist`, `/api/companies`, `/api/health`

## Tests
    python tests/test_priority_flow.py

## Notes
- Feed URLs change; check `/api/meta` -> `sources` for `last_error`.
- Reuters/Bloomberg/WSJ have no open RSS; WhatsApp/SMS need paid APIs (reachable through the generic webhook).
- Bullish/bearish labels are simple keyword rules, not trading signals.
- Store headlines/summaries + link; don't republish full article text, and check each outlet's terms.
