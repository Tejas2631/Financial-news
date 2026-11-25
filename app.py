"""Financial news aggregator: RSS collector + de-duplication + live stream + alerts + dashboard.
Run:  python -m uvicorn app:app --host 0.0.0.0 --port 8000
"""
import asyncio, calendar, hashlib, html, json, logging, os, re, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from categorizer import categorize, SECTORS, TRENDS, COMPANIES
import priority as prio
from notifier import Notifier

BASE = Path(__file__).parent
DB_PATH = os.getenv("NEWS_DB", str(BASE / "news.db"))
FEEDS_FILE = Path(os.getenv("FEEDS_FILE", str(BASE / "feeds.json")))
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL", "60"))        # seconds between checks (near real-time)
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "14"))
DEDUP_THRESHOLD = float(os.getenv("DEDUP_THRESHOLD", "0.8"))    # headline similarity that counts as "same story"
DEDUP_WINDOW_H = int(os.getenv("DEDUP_WINDOW_H", "24"))         # only compare stories published this close together
EVENT_MAX_AGE_H = int(os.getenv("EVENT_MAX_AGE_H", "6"))        # older items never trigger live events/alerts
UPDATE_COOLDOWN = int(os.getenv("UPDATE_COOLDOWN", "1800"))     # min seconds between "updated" alerts per story
BACKUP_DIR = os.getenv("BACKUP_DIR", "")                         # e.g. /data/backups ; empty = no backups
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))                # daily copies to keep
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
UA = "Mozilla/5.0 (compatible; FinNewsBot/1.0)"
log = logging.getLogger("finnews")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def _enum(name, values):
    return Enum(name, {v: v for v in values}, type=str)


Sector = _enum("Sector", list(SECTORS) + ["General"])
Trend = _enum("Trend", list(TRENDS) + ["Market Update"])
Company = _enum("Company", list(COMPANIES))
Sentiment = _enum("Sentiment", ["bullish", "bearish", "neutral"])
MinPriority = _enum("MinPriority", ["high", "medium"])
SortBy = _enum("SortBy", ["published", "priority"])
Source = _enum("Source", [f["name"] for f in json.loads(FEEDS_FILE.read_text())])
ExportFormat = _enum("ExportFormat", ["csv", "json"])
val = lambda x: x.value if isinstance(x, Enum) else x


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- de-duplication
STOP = set("a an the of in on at to for and or as is are was were be by with from its it that this".split())
TRACK = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "ocid", "spm", "ito", "guccounter"}
_lock = threading.Lock()
_recent = []      # (id, tokens, numbers, published, fingerprint) of recent stories
_seen_dup = {}    # article-url-id -> time, for URLs already known to be duplicates


def norm_url(u):
    """Same article, different link spelling (http/https, www, utm tags, trailing slash) -> same key."""
    p = urlsplit(u.strip())
    host = p.netloc.lower().removeprefix("www.")
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not (k.lower().startswith("utm_") or k.lower() in TRACK)]
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", urlencode(q), ""))


def analyze(title):
    toks = frozenset(t for t in re.findall(r"[a-z0-9]+(?:[.,][a-z0-9]+)*", title.lower().replace("’", "'"))
                     if t not in STOP and (len(t) > 1 or t.isdigit()))
    nums = frozenset(t for t in toks if any(ch.isdigit() for ch in t))
    return toks, nums, hashlib.sha1(" ".join(sorted(toks)).encode()).hexdigest()


def is_dup(toks, nums, fp, pub, cand):
    _, t2, n2, p2, fp2 = cand
    if abs(pub - p2) > DEDUP_WINDOW_H * 3600:
        return False
    if fp == fp2:
        return True
    if len(toks) < 4 or len(t2) < 4 or nums != n2:   # numbers must match: "up 200 pts" != "down 150 pts"
        return False
    inter = len(toks & t2)
    if inter / len(toks | t2) >= DEDUP_THRESHOLD:
        return True
    small, big = sorted((len(toks), len(t2)))   # one headline is the other plus a few extra words
    return small >= 5 and inter == small and big <= small * 1.6


def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS articles(
          id TEXT PRIMARY KEY, title TEXT, summary TEXT, link TEXT, source TEXT,
          published INTEGER, fetched INTEGER, sectors TEXT, trends TEXT,
          companies TEXT, sentiment TEXT);
        CREATE INDEX IF NOT EXISTS idx_pub ON articles(published DESC);
        CREATE TABLE IF NOT EXISTS sources(
          name TEXT PRIMARY KEY, url TEXT, etag TEXT, modified TEXT,
          last_ok INTEGER, last_error TEXT, total INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, article_id TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(articles)")}
        for col, typ in (("fp", "TEXT"), ("also_in", "TEXT"), ("updated", "INTEGER"), ("base", "INTEGER")):
            if col not in cols:
                c.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
        c.commit()
    dedupe_existing()
    backfill_priority()
    load_recent()


def dedupe_existing():
    """One-time cleanup of an older database: merge duplicates, switch to normalized ids."""
    with closing(db()) as c:
        rows = c.execute("SELECT id,title,link,source,published FROM articles WHERE fp IS NULL ORDER BY published").fetchall()
        if not rows:
            return
        kept, srcs, also, removed = [], {}, {}, 0
        for r in rows:
            toks, nums, fp = analyze(r["title"])
            hit = None
            for k in reversed(kept):
                if r["published"] - k[3] > DEDUP_WINDOW_H * 3600:
                    break
                if is_dup(toks, nums, fp, r["published"], k):
                    hit = k; break
            if hit:
                if r["source"] != srcs[hit[0]]:
                    also[hit[0]].add(r["source"])
                c.execute("DELETE FROM articles WHERE id=?", (r["id"],)); removed += 1
                continue
            new_id = hashlib.sha1(norm_url(r["link"]).encode()).hexdigest()
            try:
                c.execute("UPDATE articles SET id=?, fp=? WHERE id=?", (new_id, fp, r["id"]))
            except sqlite3.IntegrityError:
                c.execute("DELETE FROM articles WHERE id=?", (r["id"],)); removed += 1
                continue
            kept.append((new_id, toks, nums, r["published"], fp)); srcs[new_id] = r["source"]; also[new_id] = set()
        for aid, s in also.items():
            if s:
                c.execute("UPDATE articles SET also_in=? WHERE id=?", (pack(sorted(s)), aid))
        c.commit()
    log.info("cleanup: merged %d duplicate articles", removed)


def split_tags(s):
    return [x for x in (s or "").split("|") if x]


def base_of(title, sectors, trends, companies, sentiment, also_in):
    """Priority base score (0-80) from stored tag strings; also_in counts the extra sites."""
    return prio.base_score(title, split_tags(trends), split_tags(companies), sentiment, 1 + len(split_tags(also_in)))


def backfill_priority():
    """Score stories stored before priority existed."""
    with closing(db()) as c:
        rows = c.execute("SELECT id,title,sectors,trends,companies,sentiment,also_in FROM articles WHERE base IS NULL").fetchall()
        for r in rows:
            c.execute("UPDATE articles SET base=? WHERE id=?",
                      (base_of(r["title"], r["sectors"], r["trends"], r["companies"], r["sentiment"], r["also_in"]), r["id"]))
        c.commit()
    if rows:
        log.info("priority: scored %d existing stories", len(rows))


def rescore(c, aid):
    r = c.execute("SELECT title,sectors,trends,companies,sentiment,also_in FROM articles WHERE id=?", (aid,)).fetchone()
    if r:
        c.execute("UPDATE articles SET base=? WHERE id=?",
                  (base_of(r["title"], r["sectors"], r["trends"], r["companies"], r["sentiment"], r["also_in"]), aid))


def load_recent():
    cutoff = int(time.time()) - DEDUP_WINDOW_H * 3600 * 2
    with closing(db()) as c:
        rows = c.execute("SELECT id,title,published FROM articles WHERE published>=?", (cutoff,)).fetchall()
    items = []
    for r in rows:
        toks, nums, fp = analyze(r["title"])
        items.append((r["id"], toks, nums, r["published"], fp))
    with _lock:
        _recent[:] = items


# ---------------------------------------------------------------- collecting
def clean(text, n=280):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


def pack(items):  # "|a|b|" so LIKE '%|a|%' matches whole tags
    return "|" + "|".join(items) + "|" if items else "|"


def add_event(c, kind, aid, now):
    c.execute("INSERT INTO events(type,article_id,ts) VALUES(?,?,?)", (kind, aid, now))


def store_entry(c, name, e, now):
    """Returns 'new', 'update', 'dup' or 'seen'. Caller holds _lock."""
    link = (e.get("link") or "").strip()
    title = clean(e.get("title"), 300)
    if not link or not title:
        return None
    ts = e.get("published_parsed") or e.get("updated_parsed")
    published = min(calendar.timegm(ts), now) if ts else now
    aid = hashlib.sha1(norm_url(link).encode()).hexdigest()
    if aid in _seen_dup:
        return "dup"
    toks, nums, fp = analyze(title)
    fresh = published >= now - EVENT_MAX_AGE_H * 3600
    summary = clean(e.get("summary") or e.get("description"))

    row = c.execute("SELECT fp, updated FROM articles WHERE id=?", (aid,)).fetchone()
    if row:                                     # same article seen before
        if row["fp"] and row["fp"] != fp:       # ...but the publisher changed the headline
            sectors, trends, companies, sentiment = categorize(title, summary)
            c.execute("UPDATE articles SET title=?,summary=?,sectors=?,trends=?,companies=?,sentiment=?,fp=?,updated=? WHERE id=?",
                      (title, summary, pack(sectors), pack(trends), pack(companies), sentiment, fp, now, aid))
            rescore(c, aid)
            for i, x in enumerate(_recent):
                if x[0] == aid:
                    _recent[i] = (aid, toks, nums, x[3], fp)
            if fresh and (row["updated"] is None or now - row["updated"] >= UPDATE_COOLDOWN):
                add_event(c, "update", aid, now)
            return "update"
        return "seen"

    dup = next((x for x in reversed(_recent) if is_dup(toks, nums, fp, published, x)), None)
    if dup:                                     # same story already stored (maybe from another site)
        _seen_dup[aid] = now
        r2 = c.execute("SELECT source, also_in FROM articles WHERE id=?", (dup[0],)).fetchone()
        if r2 and name != r2["source"] and f"|{name}|" not in (r2["also_in"] or "|"):
            members = [s for s in (r2["also_in"] or "").split("|") if s] + [name]
            c.execute("UPDATE articles SET also_in=? WHERE id=?", (pack(members), dup[0]))
            rescore(c, dup[0])        # more sites carrying the story = higher priority
        return "dup"

    sectors, trends, companies, sentiment = categorize(title, summary)
    base = prio.base_score(title, trends, companies, sentiment, 1)
    c.execute("INSERT INTO articles(id,title,summary,link,source,published,fetched,sectors,trends,companies,sentiment,fp,also_in,updated,base) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
              (aid, title, summary, link, name, published, now, pack(sectors), pack(trends),
               pack(companies), sentiment, fp, "|", base))
    _recent.append((aid, toks, nums, published, fp))
    if fresh:
        add_event(c, "new", aid, now)
    return "new"


def fetch_one(src):
    name, url = src["name"], src["url"]
    with closing(db()) as c:
        row = c.execute("SELECT etag, modified FROM sources WHERE name=?", (name,)).fetchone()
    try:
        d = feedparser.parse(url, agent=UA,
                             etag=row["etag"] if row else None,
                             modified=row["modified"] if row else None)
        if getattr(d, "status", 200) == 304:
            return name, 0, None
        if d.bozo and not d.entries:
            raise ValueError(str(getattr(d, "bozo_exception", "invalid feed")))
        now, new = int(time.time()), 0
        with _lock, closing(db()) as c:
            for e in d.entries:
                if store_entry(c, name, e, now) == "new":
                    new += 1
            c.execute("""INSERT INTO sources(name,url,etag,modified,last_ok,last_error,total)
                         VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(name) DO UPDATE SET
                         etag=excluded.etag, modified=excluded.modified, last_ok=excluded.last_ok,
                         last_error=NULL, total=sources.total+excluded.total""",
                      (name, url, d.get("etag"), d.get("modified"), now, new))
            c.commit()
        return name, new, None
    except Exception as ex:
        with closing(db()) as c:
            c.execute("""INSERT INTO sources(name,url,last_error) VALUES(?,?,?)
                         ON CONFLICT(name) DO UPDATE SET last_error=excluded.last_error""",
                      (name, url, str(ex)[:200]))
            c.commit()
        return name, 0, str(ex)


def run_cycle():
    feeds = json.loads(FEEDS_FILE.read_text())  # re-read: edit without restart
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_one, feeds))
    log.info("cycle: +%d new, %d failing", sum(r[1] for r in results), sum(1 for r in results if r[2]))
    now = int(time.time())
    with _lock:
        cutoff = now - DEDUP_WINDOW_H * 3600 * 2
        _recent[:] = [x for x in _recent if x[3] >= cutoff]
        for k in [k for k, t in _seen_dup.items() if t < cutoff]:
            del _seen_dup[k]
    with closing(db()) as c:
        c.execute("DELETE FROM articles WHERE published < ?", (now - RETENTION_DAYS * 86400,))
        c.execute("DELETE FROM events WHERE ts < ?", (now - 3 * 86400,))
        if not c.execute("SELECT 1 FROM kv WHERE k='notify_cursor'").fetchone():
            top = c.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            c.execute("INSERT INTO kv VALUES('notify_cursor', ?)", (str(top),))  # skip first-run backlog
        c.commit()
    try:
        backup_db()
    except Exception:
        log.exception("backup failed")


def backup_db():
    """One consistent copy of news.db per day (SQLite online backup), newest BACKUP_KEEP kept."""
    if not BACKUP_DIR:
        return
    d = Path(BACKUP_DIR); d.mkdir(parents=True, exist_ok=True)
    dest = d / f"news-{time.strftime('%Y%m%d', time.gmtime())}.db"
    if dest.exists():
        return
    tmp = dest.with_suffix(".tmp")
    with closing(db()) as src, closing(sqlite3.connect(tmp)) as dst:
        src.backup(dst)
    tmp.replace(dest)
    for old in sorted(d.glob("news-*.db"))[:-BACKUP_KEEP]:
        old.unlink()
    log.info("backup written: %s", dest)


def loop(stop):
    while not stop.is_set():
        try:
            run_cycle()
        except Exception:
            log.exception("cycle failed")
        stop.wait(FETCH_INTERVAL)


# ---------------------------------------------------------------- API
def row_out(r):
    split = split_tags
    companies = split(r["companies"])
    score = prio.final_score(r["base"], companies)
    return {"id": r["id"], "title": r["title"], "summary": r["summary"], "link": r["link"],
            "source": r["source"], "also_reported_by": split(r["also_in"]),
            "published": r["published"], "updated": r["updated"], "sentiment": r["sentiment"],
            "sectors": split(r["sectors"]), "trends": split(r["trends"]), "companies": companies,
            "priority": {"score": score, "level": prio.level_of(score), "watch": bool(prio.watch_hits(companies))}}


notifier = Notifier(db, row_out, os.getenv("NOTIFY_CONFIG", str(BASE / "notify.json")), log)


@asynccontextmanager
async def lifespan(_):
    init_db()
    stop = threading.Event()
    threading.Thread(target=loop, args=(stop,), daemon=True).start()
    threading.Thread(target=notifier.loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="Financial News Aggregator", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST", "PUT", "DELETE"],
                   allow_headers=["X-API-Key", "Content-Type"])


def prio_sql(alias="", min_priority=None, watch_only=False):
    """SQL pieces for the live score = stored base + watchlist bonus.
    Returns (score_expr, score_args, where_clauses, where_args)."""
    p = alias + "." if alias else ""
    names = sorted(prio.load_watchlist())
    hit = " OR ".join(f"LOWER({p}companies) LIKE ?" for _ in names)
    hit_args = [f"%|{n.lower()}|%" for n in names]
    score = f"(COALESCE({p}base,0) + CASE WHEN ({hit}) THEN {prio.WL_BONUS} ELSE 0 END)" if names else f"COALESCE({p}base,0)"
    where, args = [], []
    if min_priority:
        where.append(f"{score} >= ?"); args += hit_args + [prio.LEVEL_MIN[val(min_priority)]]
    if watch_only:
        where.append(f"({hit})" if names else "0"); args += hit_args
    return score, hit_args, where, args


def _search(sector=None, trend=None, company=None, source=None, sentiment=None,
            q=None, hours=None, limit=50, offset=0, min_priority=None, watch_only=False, sort="published"):
    where, args = [], []
    _, hit_args, pw, pa = prio_sql("", min_priority, watch_only)
    where += pw; args += pa
    for col, v in (("sectors", sector), ("trends", trend), ("companies", company)):
        if v:
            where.append(f"{col} LIKE ?"); args.append(f"%|{val(v)}|%")
    if source: where.append("source=?"); args.append(val(source))
    if sentiment: where.append("sentiment=?"); args.append(val(sentiment))
    if q: where.append("(title LIKE ? OR summary LIKE ? OR companies LIKE ?)"); args += [f"%{q}%"] * 3
    if hours: where.append("published >= ?"); args.append(int(time.time()) - hours * 3600)
    sql = "SELECT * FROM articles" + (" WHERE " + " AND ".join(where) if where else "")
    order, oargs = "published DESC", []
    if val(sort) == "priority":
        sc, oargs, _, _ = prio_sql()
        order = f"{sc} DESC, published DESC"
    with closing(db()) as c:
        total = c.execute(sql.replace("*", "COUNT(*)", 1), args).fetchone()[0]
        rows = c.execute(sql + f" ORDER BY {order} LIMIT ? OFFSET ?", args + oargs + [limit, offset]).fetchall()
    return {"total": total, "items": [row_out(r) for r in rows]}


@app.get("/api/news", summary="Get news articles")
def news(
    sector: Optional[Sector] = Query(None, description="Pick an industry sector. Leave empty for all sectors."),
    trend: Optional[Trend] = Query(None, description="Pick a market trend or topic (Rally, Earnings, IPO...). Leave empty for all."),
    company: Optional[Company] = Query(None, description="Pick a company mentioned in the story. Leave empty for all."),
    source: Optional[Source] = Query(None, description="Pick the site that published it first. Leave empty for all sources."),
    sentiment: Optional[Sentiment] = Query(None, description="bullish = positive headline, bearish = negative, neutral = neither."),
    q: Optional[str] = Query(None, description="Type any word to search in headline, summary and company tags. Example: inflation"),
    hours: Optional[int] = Query(None, ge=1, description="Only news from the last N hours. Example: 24 = last day, 168 = last week. Empty = all stored news."),
    min_priority: Optional[MinPriority] = Query(None, description="high = only high-priority stories; medium = medium and high."),
    watchlist: bool = Query(False, description="true = only stories that mention a company on your watchlist."),
    sort: SortBy = Query(SortBy.published, description="published = newest first; priority = most important first."),
    limit: int = Query(50, ge=1, le=200, description="How many articles to return (1 to 200)."),
    offset: int = Query(0, ge=0, description="Skip this many articles. Use 50, 100... to get the next page."),
):
    """Each story appears once. `also_reported_by` lists the other sites that carried the same story.
    `priority` = {score, level (high/medium/low), watch (mentions a watchlist company)}."""
    return _search(sector, trend, company, source, sentiment, q, hours, limit, offset, min_priority, watchlist, sort)


@app.get("/api/export", summary="Download news as CSV or JSON")
def export(
    format: ExportFormat = Query(ExportFormat.csv, description="csv opens in Excel. json is for programs."),
    hours: int = Query(24, ge=1, description="News from the last N hours. Example: 24 = last day, 168 = last week."),
    sector: Optional[Sector] = Query(None, description="Pick an industry sector. Leave empty for all."),
    trend: Optional[Trend] = Query(None, description="Pick a market trend or topic. Leave empty for all."),
    company: Optional[Company] = Query(None, description="Pick a company. Leave empty for all."),
    source: Optional[Source] = Query(None, description="Pick a news website. Leave empty for all."),
    sentiment: Optional[Sentiment] = Query(None, description="bullish, bearish or neutral. Leave empty for all."),
    q: Optional[str] = Query(None, description="Search word in headline and summary. Example: inflation"),
    min_priority: Optional[MinPriority] = Query(None, description="high or medium (medium includes high). Empty = all."),
    watchlist: bool = Query(False, description="true = only watchlist companies."),
    sort: SortBy = Query(SortBy.published, description="published or priority."),
    limit: int = Query(1000, ge=1, le=5000, description="Maximum articles to export (1 to 5000)."),
):
    data = _search(sector, trend, company, source, sentiment, q, hours, limit, 0, min_priority, watchlist, sort)
    items = data["items"]
    if format == ExportFormat.json:
        return {"total": data["total"], "items": items}
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["published_utc", "source", "also_reported_by", "title", "summary", "link",
                "sectors", "trends", "companies", "sentiment", "updated_utc", "priority", "priority_score", "watchlist"])
    fmt_t = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) if t else ""
    for a in items:
        w.writerow([fmt_t(a["published"]), a["source"], ";".join(a["also_reported_by"]), a["title"],
                    a["summary"], a["link"], ";".join(a["sectors"]), ";".join(a["trends"]),
                    ";".join(a["companies"]), a["sentiment"], fmt_t(a["updated"]),
                    a["priority"]["level"], a["priority"]["score"], "yes" if a["priority"]["watch"] else ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=financial_news.csv"})


def _events_after(cursor, limit, sector=None, trend=None, company=None, sentiment=None, min_priority=None, watch_only=False):
    where, args = ["e.seq > ?"], [cursor]
    _, _, pw, pa = prio_sql("a", min_priority, watch_only)
    where += pw; args += pa
    for col, v in (("sectors", sector), ("trends", trend), ("companies", company)):
        if v:
            where.append(f"a.{col} LIKE ?"); args.append(f"%|{val(v)}|%")
    if sentiment: where.append("a.sentiment=?"); args.append(val(sentiment))
    with closing(db()) as c:
        top = c.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        rows = c.execute("SELECT e.seq AS seq, e.type AS etype, a.* FROM events e JOIN articles a ON a.id=e.article_id "
                         f"WHERE {' AND '.join(where)} AND e.seq <= ? ORDER BY e.seq LIMIT ?",
                         args + [top, limit]).fetchall()
    out = [{"seq": r["seq"], "type": r["etype"], "article": row_out(r)} for r in rows]
    return out, (rows[-1]["seq"] if len(rows) >= limit else max(top, cursor))


@app.get("/api/events", summary="Poll for new/updated news since a point in time")
def events(
    after: int = Query(0, ge=0, description="Return events after this number. Start with 0, then pass back `last_seq` each time."),
    limit: int = Query(100, ge=1, le=500, description="Maximum events per call."),
    sector: Optional[Sector] = Query(None, description="Only this sector. Empty = all."),
    trend: Optional[Trend] = Query(None, description="Only this trend. Empty = all."),
    company: Optional[Company] = Query(None, description="Only this company. Empty = all."),
    sentiment: Optional[Sentiment] = Query(None, description="Only this sentiment. Empty = all."),
    min_priority: Optional[MinPriority] = Query(None, description="high or medium (medium includes high). Empty = all."),
    watchlist: bool = Query(False, description="true = only watchlist companies."),
):
    """type `new` = a story just arrived. type `update` = the publisher changed a headline."""
    evs, last = _events_after(after, limit, sector, trend, company, sentiment, min_priority, watchlist)
    return {"events": evs, "last_seq": last}


@app.get("/api/stream", summary="Live news stream (Server-Sent Events)")
async def stream(
    request: Request,
    sector: Optional[Sector] = Query(None, description="Only this sector. Empty = all."),
    trend: Optional[Trend] = Query(None, description="Only this trend. Empty = all."),
    company: Optional[Company] = Query(None, description="Only this company. Empty = all."),
    sentiment: Optional[Sentiment] = Query(None, description="Only this sentiment. Empty = all."),
    min_priority: Optional[MinPriority] = Query(None, description="high or medium (medium includes high). Empty = all."),
    watchlist: bool = Query(False, description="true = only watchlist companies."),
    replay: int = Query(0, ge=0, le=200, description="Also send the last N events first. 0 = only brand-new news."),
    last_event_id: Optional[str] = Header(None),
):
    """Keeps the connection open and pushes each new or updated story the moment it is stored.
    Don't use 'Try it out' here. Run `python listen.py` or open it from your own code (EventSource)."""
    with closing(db()) as c:
        top = c.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        cursor = top
        if last_event_id and last_event_id.isdigit():
            cursor = int(last_event_id)
        elif replay:
            r = c.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?", (replay - 1,)).fetchone()
            cursor = r[0] - 1 if r else 0

    async def gen():
        cur, last = cursor, time.time()
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            evs, cur = await asyncio.to_thread(_events_after, cur, 100, sector, trend, company, sentiment, min_priority, watchlist)
            for ev in evs:
                yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev['article'], ensure_ascii=False)}\n\n"
                last = time.time()
            if time.time() - last > 15:
                yield ": ping\n\n"; last = time.time()
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/notify/test", summary="Send a test alert to every channel in notify.json")
async def notify_test():
    return {"results": await asyncio.to_thread(notifier.send_test)}


@app.get("/api/meta", summary="Source health and tag counts")
def meta():
    counts = {"sectors": {}, "trends": {}, "companies": {}}
    with closing(db()) as c:
        for r in c.execute("SELECT sectors, trends, companies FROM articles WHERE published >= ?",
                           (int(time.time()) - 86400 * 3,)):
            for key in counts:
                for t in r[key].split("|"):
                    if t: counts[key][t] = counts[key].get(t, 0) + 1
        srcs = [dict(r) for r in c.execute("SELECT name,last_ok,last_error,total FROM sources ORDER BY name")]
        total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    top = lambda d, n=None: dict(sorted(d.items(), key=lambda kv: -kv[1])[:n])
    return {"total": total, "sectors": top(counts["sectors"]), "trends": top(counts["trends"]),
            "companies": top(counts["companies"], 15), "sources": srcs}


@app.post("/api/refresh", summary="Fetch all feeds now")
def refresh():
    threading.Thread(target=run_cycle, daemon=True).start()
    return {"status": "refresh started"}


def _canon(names):
    """Match names to known companies, case-insensitively. Returns (valid, unknown)."""
    known = {k.lower(): k for k in COMPANIES}
    ok, bad = [], []
    for n in names:
        k = known.get(str(n).strip().lower())
        (ok if k else bad).append(k or str(n))
    return sorted(set(ok)), bad


@app.get("/api/companies", summary="All companies that can be tagged / put on the watchlist")
def companies():
    return {"companies": sorted(COMPANIES)}


@app.get("/api/watchlist", summary="Companies on your watchlist")
def get_watchlist():
    return {"companies": sorted(prio.load_watchlist())}


@app.put("/api/watchlist", summary="Replace the whole watchlist")
def put_watchlist(body: dict):
    """Body: {"companies": ["Reliance", "TCS"]}. Names must exist in /api/companies."""
    ok, bad = _canon(body.get("companies", []))
    if bad:
        raise HTTPException(422, f"Unknown companies: {bad}. See /api/companies.")
    prio.save_watchlist(ok)
    return {"companies": ok}


@app.post("/api/watchlist/{name}", summary="Add one company to the watchlist")
def add_watch(name: str):
    ok, bad = _canon([name])
    if bad:
        raise HTTPException(404, f"Unknown company: {name}. See /api/companies.")
    prio.save_watchlist(set(prio.load_watchlist()) | set(ok))
    return {"companies": sorted(prio.load_watchlist())}


@app.delete("/api/watchlist/{name}", summary="Remove one company from the watchlist")
def del_watch(name: str):
    prio.save_watchlist({n for n in prio.load_watchlist() if n.lower() != name.strip().lower()})
    return {"companies": sorted(prio.load_watchlist())}


@app.get("/api/health", summary="Health check (used by Docker)")
def health():
    with closing(db()) as c:
        c.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(BASE / "dashboard.html")
