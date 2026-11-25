"""End-to-end check of priority + watchlist + alerts (no network). Run:  python tests/test_priority_flow.py"""
import json, os, sys, tempfile, time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(NEWS_DB=str(tmp / "news.db"), WATCHLIST_FILE=str(tmp / "watchlist.json"),
                  NOTIFY_CONFIG=str(tmp / "notify.json"), BACKUP_DIR=str(tmp / "bk"))
(tmp / "watchlist.json").write_text(json.dumps({"companies": ["Reliance", "TCS", "HDFC Bank", "Infosys", "Tata Motors"]}))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A                      # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402
from contextlib import closing       # noqa: E402
import notifier as N                 # noqa: E402

A.init_db()
now = int(time.time())


def feed(source, title, link, summary="", age=60):
    t = time.gmtime(now - age)
    with A._lock, closing(A.db()) as c:
        r = A.store_entry(c, source, {"title": title, "link": link, "summary": summary, "published_parsed": t}, now)
        c.commit()
    return r


assert feed("Moneycontrol Latest", "Reliance Industries Q2 profit jumps 18%, beats estimates", "https://a.com/1") == "new"
assert feed("Livemint Markets", "Sensex ends flat, Nifty steady in range-bound session", "https://b.com/2") == "new"
assert feed("CNBC Finance", "Nvidia shares surge after earnings guidance beat", "https://c.com/3") == "new"
assert feed("Economic Times Markets", "Breaking: RBI cuts repo rate by 25 bps in surprise move", "https://d.com/4") == "new"
assert feed("Business Standard Markets", "Reliance Industries Q2 profit jumps 18%, beats estimates: report", "https://e.com/5") == "dup"

c = TestClient(A.app)
items = c.get("/api/news").json()["items"]
by = {i["title"][:12]: i for i in items}
rel = by["Reliance Ind"]
print("Reliance:", rel["priority"], rel["companies"], "also:", rel["also_reported_by"])
assert rel["priority"]["watch"] is True and rel["priority"]["level"] == "high", rel["priority"]   # watch bonus + 2 sources + earnings
assert by["Sensex ends "]["priority"]["level"] == "low"
assert by["Nvidia share"]["priority"]["watch"] is False
rbi = by["Breaking: RB"]["priority"]; print("RBI:", rbi)

assert [i["title"][:8] for i in c.get("/api/news?sort=priority").json()["items"]][0] in ("Reliance", "Breaking")
hi = c.get("/api/news?min_priority=high").json()
assert all(i["priority"]["level"] == "high" for i in hi["items"]) and hi["total"] >= 1
md = c.get("/api/news?min_priority=medium").json()["total"]; assert md >= hi["total"]
w = c.get("/api/news?watchlist=true").json()
assert w["total"] == 1 and w["items"][0]["companies"] == ["Reliance"]
assert c.get("/api/news?watchlist=true&min_priority=high&sort=priority&company=Reliance").json()["total"] == 1
csv = c.get("/api/export?format=csv&hours=24&sort=priority").text
assert "priority_score" in csv.splitlines()[0]; print("csv ok")

# watchlist API: removing Reliance re-ranks the stored story immediately
assert c.delete("/api/watchlist/Reliance").json()["companies"] == ["HDFC Bank", "Infosys", "TCS", "Tata Motors"]
rel2 = [i for i in c.get("/api/news").json()["items"] if i["title"].startswith("Reliance")][0]
assert rel2["priority"]["watch"] is False and rel2["priority"]["score"] == rel["priority"]["score"] - 35
assert c.get("/api/news?watchlist=true").json()["total"] == 0
assert c.post("/api/watchlist/reliance").json()["companies"][-1] == "Reliance" or "Reliance" in c.get("/api/watchlist").json()["companies"]
assert c.put("/api/watchlist", json={"companies": ["Nope Inc"]}).status_code == 422
assert c.put("/api/watchlist", json={"companies": ["infosys", "tcs"]}).json()["companies"] == ["Infosys", "TCS"]
assert "Reliance" in c.get("/api/companies").json()["companies"] and len(c.get("/api/companies").json()["companies"]) > 250
c.put("/api/watchlist", json={"companies": ["Reliance", "TCS", "HDFC Bank", "Infosys", "Tata Motors"]})

# events/stream filters
ev = c.get("/api/events?after=0&min_priority=high").json()["events"]
assert ev and all(e["article"]["priority"]["level"] == "high" for e in ev)
assert len(c.get("/api/events?after=0&watchlist=true").json()["events"]) == 1

# notifier filters
evs = c.get("/api/events?after=0").json()["events"]
assert {len(x) for x in [[e for e in evs if N.match(e, {"min_priority": "high"})]]} and \
       all(e["article"]["priority"]["level"] == "high" for e in evs if N.match(e, {"min_priority": "high"}))
assert [e["article"]["companies"] for e in evs if N.match(e, {"watchlist_only": True})] == [["Reliance"]]
print(N.fmt([e for e in evs if N.match(e, {"watchlist_only": True})][0]))

# backfill of a pre-priority database
with closing(A.db()) as d:
    d.execute("UPDATE articles SET base=NULL"); d.commit()
A.backfill_priority()
assert c.get("/api/news?min_priority=high").json()["total"] == hi["total"]

# backup: once a day, restorable
A.backup_db(); A.backup_db()
files = list((tmp / "bk").glob("news-*.db")); assert len(files) == 1
import sqlite3; assert sqlite3.connect(files[0]).execute("select count(*) from articles").fetchone()[0] == 4
assert c.get("/api/health").json() == {"ok": True}
print("ALL OK")
