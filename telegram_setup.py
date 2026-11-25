"""Finds your Telegram chat_id and writes notify.json for you.

1. In Telegram, talk to @BotFather -> /newbot -> copy the token.
2. Open your new bot in Telegram and send it any message (e.g. "hi").
3. Run:  python telegram_setup.py <bot-token> [high|medium|all|watchlist]
   high      = only high-priority stories (default)
   medium    = medium and high
   all       = every story
   watchlist = every story that mentions a company on your watchlist
It sends a test message and saves notify.json (an existing notify.json is backed up).
For Docker, run it once on any computer and copy notify.json into the ./data folder on the server.
"""
import json, sys, urllib.request
from pathlib import Path

if len(sys.argv) < 2:
    sys.exit(__doc__)
token, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "high")
api = f"https://api.telegram.org/bot{token}"


def call(method, payload=None):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(f"{api}/{method}", data, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))


me = call("getMe")["result"]
chats = {}
for u in call("getUpdates")["result"]:
    m = u.get("message") or u.get("channel_post") or {}
    if m.get("chat"):
        chats[m["chat"]["id"]] = m["chat"].get("title") or m["chat"].get("first_name") or "chat"
if not chats:
    sys.exit(f"No messages found. Open t.me/{me['username']}, send it a message, then run this again.")
chat_id, name = list(chats.items())[-1]
flt = {"high": {"min_priority": "high"}, "medium": {"min_priority": "medium"}, "all": {},
       "watchlist": {"watchlist_only": True}}.get(mode)
if flt is None:
    sys.exit("mode must be high, medium, all or watchlist")
cfg = {"channels": [{"type": "telegram", "enabled": True, "bot_token": token, "chat_id": str(chat_id), "filter": flt}]}
out = Path(__file__).parent / "notify.json"
if out.exists():
    out.replace(out.with_suffix(".json.bak"))
out.write_text(json.dumps(cfg, indent=2))
call("sendMessage", {"chat_id": chat_id, "text": f"Connected. You will get '{mode}' alerts here."})
print(f"Saved {out} for chat '{name}' ({chat_id}). Check Telegram for the confirmation message.")
