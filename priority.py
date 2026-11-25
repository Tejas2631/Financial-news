"""News priority: which stories matter most, plus the watchlist (companies you follow)."""
import json, os, re
from pathlib import Path

BASE = Path(__file__).parent
WATCHLIST_FILE = Path(os.getenv("WATCHLIST_FILE", str(BASE / "watchlist.json")))
WL_BONUS = 35                                   # added when a story mentions a watchlist company
LEVELS = {"low": 0, "medium": 1, "high": 2}
LEVEL_MIN = {"high": 60, "medium": 35}          # score needed for each level
TREND_WEIGHT = {"Earnings": 15, "M&A": 15, "Rates & Policy": 15, "IPO & Listings": 12, "Selloff": 12,
                "Rally": 10, "Index Moves": 8, "FII/DII Flows": 6, "Currency": 5}
BREAKING = re.compile(r"\b(breaking|just in|flash|alert|exclusive|urgent)\b", re.I)


def base_score(title, trends, companies, sentiment, n_sources):
    """0-80 from the story itself: topic weight, named company, 'breaking' wording,
    strong tone, and how many different sites carry it."""
    s = min(25, sum(TREND_WEIGHT.get(t, 0) for t in trends))
    if companies: s += 8
    if BREAKING.search(title): s += 15
    if sentiment != "neutral": s += 5
    s += min(30, 10 * (n_sources - 1))
    return min(s, 80)


def watch_hits(companies, names=None):
    """Which of the story's companies are on the watchlist (case-insensitive)."""
    wl = {n.lower() for n in (load_watchlist() if names is None else names)}
    return [c for c in companies if c.lower() in wl]


def final_score(base, companies, names=None):
    """Stored base score + the watchlist bonus. Computed when read, so editing the
    watchlist re-ranks old stories too."""
    return (base or 0) + (WL_BONUS if watch_hits(companies, names) else 0)


def level_of(score):
    return "high" if score >= LEVEL_MIN["high"] else "medium" if score >= LEVEL_MIN["medium"] else "low"


_cache = {"mtime": None, "names": set()}


def load_watchlist():
    """Re-read watchlist.json only when it changes, so edits apply instantly."""
    try:
        m = WATCHLIST_FILE.stat().st_mtime
    except FileNotFoundError:
        return set()
    if _cache["mtime"] != m:
        try:
            _cache["names"] = set(json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")).get("companies", []))
        except Exception:
            _cache["names"] = set()
        _cache["mtime"] = m
    return _cache["names"]


def save_watchlist(names):
    WATCHLIST_FILE.write_text(json.dumps({"companies": sorted(names)}, indent=2), encoding="utf-8")
    _cache["mtime"] = None
