"""Push alerts for new/updated news. Channels are configured in notify.json
(see notify.example.json). The file is re-read every pass, so edits apply without a restart."""
import hashlib, hmac, json, smtplib, time, urllib.request
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path


def post_raw(url, data: bytes, headers=None, timeout=10):
    req = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})})
    urllib.request.urlopen(req, timeout=timeout).read()


def chunks(text, n):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n and cur:
            out.append(cur); cur = ""
        cur += line + "\n"
    return out + ([cur.rstrip()] if cur.strip() else [])


def tag(ev):
    return "UPDATED" if ev["type"] == "update" else "NEW"


def prio_tag(a):
    p = a.get("priority") or {}
    lvl = {"high": "HIGH", "medium": "MEDIUM"}.get(p.get("level"), "")
    return ("\u2605 " if p.get("watch") else "") + (f"{lvl} " if lvl else "")


def fmt(ev):
    a = ev["article"]
    tags = ", ".join(a["companies"] + a["sectors"][:2] + a["trends"][:2])
    return f"{prio_tag(a)}[{tag(ev)}] {a['source']}: {a['title']}\n{a['link']}" + (f"\n{tags}" if tags else "")


def texts(evs, digest_over):
    if len(evs) > digest_over:  # too many at once: send one summary instead of a flood
        lines = [f"{len(evs)} market news updates:"]
        evs = sorted(evs, key=lambda e: -(e["article"].get("priority") or {}).get("score", 0))  # most important first
        for e in evs[:10]:
            a = e["article"]
            lines.append(f"- {prio_tag(a)}[{tag(e)}] {a['source']}: {a['title']}\n  {a['link']}")
        if len(evs) > 10:
            lines.append(f"...and {len(evs) - 10} more")
        return ["\n".join(lines)]
    return [fmt(e) for e in evs]


RANK = {"low": 0, "medium": 1, "high": 2}


def match(ev, f):
    a = ev["article"]
    if f.get("events") and ev["type"] not in f["events"]:
        return False
    for key, field in (("companies", "companies"), ("sectors", "sectors"), ("trends", "trends")):
        want = {x.lower() for x in f.get(key, [])}
        if want and not want & {x.lower() for x in a[field]}:
            return False
    if f.get("sentiment") and a["sentiment"] != f["sentiment"]:
        return False
    p = a.get("priority") or {}
    if f.get("watchlist_only") and not p.get("watch"):
        return False
    want = f.get("min_priority")             # "high" or "medium" (medium also lets high through)
    if want and RANK.get(p.get("level"), 0) < RANK.get(want, 0):
        return False
    kws = [k.lower() for k in f.get("keywords", [])]
    if kws and not any(k in (a["title"] + " " + a["summary"]).lower() for k in kws):
        return False
    return True


class Notifier:
    def __init__(self, db, row_out, cfg_path, log):
        self.db, self.row_out, self.cfg_path, self.log = db, row_out, Path(cfg_path), log
        self._last_err = None

    def load(self):
        try:
            cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            self._last_err = None
        except FileNotFoundError:
            return []
        except Exception as ex:
            if str(ex) != self._last_err:
                self.log.warning("notify.json problem: %s", ex)
                self._last_err = str(ex)
            return []
        return [c for c in cfg.get("channels", []) if c.get("enabled", True)]

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.pass_once()
            except Exception:
                self.log.exception("notifier pass failed")
            stop.wait(5)

    def pass_once(self):
        with closing(self.db()) as c:
            cur = c.execute("SELECT v FROM kv WHERE k='notify_cursor'").fetchone()
            if not cur:  # set after the first fetch cycle so old backlog is never pushed
                return
            rows = c.execute(
                "SELECT e.seq AS seq, e.type AS etype, a.* FROM events e "
                "JOIN articles a ON a.id=e.article_id WHERE e.seq>? ORDER BY e.seq LIMIT 200",
                (int(cur["v"]),)).fetchall()
            if not rows:
                return
            evs = [{"seq": r["seq"], "type": r["etype"], "article": self.row_out(r)} for r in rows]
            c.execute("UPDATE kv SET v=? WHERE k='notify_cursor'", (str(evs[-1]["seq"]),))
            c.commit()  # advance first: at-most-once delivery, never a repeat flood
        for ch in self.load():
            mine = [e for e in evs if match(e, ch.get("filter", {}))]
            if not mine:
                continue
            try:
                self.deliver(ch, mine)
                self.log.info("notified %s: %d item(s)", ch.get("type"), len(mine))
            except Exception as ex:
                self.log.warning("notify %s failed: %s", ch.get("type"), ex)

    def deliver(self, ch, evs):
        kind = ch.get("type")
        if kind == "webhook":
            self.send_webhook(ch, {"events": evs})
        elif kind == "email":
            self.send_email(ch, f"Market news: {len(evs)} update(s)", "\n\n".join(texts(evs, 10**6)))
        else:
            msgs = texts(evs, ch.get("digest_over", 8))
            for i, m in enumerate(msgs):
                self.send_text(ch, m)
                if i < len(msgs) - 1:
                    time.sleep(1)  # Telegram/Discord rate limits

    def send_text(self, ch, text):
        kind = ch.get("type")
        if kind == "telegram":
            for part in chunks(text, 3900):
                post_raw(f"https://api.telegram.org/bot{ch['bot_token']}/sendMessage",
                         json.dumps({"chat_id": ch["chat_id"], "text": part,
                                     "disable_web_page_preview": True}).encode())
        elif kind == "discord":
            for part in chunks(text, 1900):
                post_raw(ch["webhook_url"], json.dumps({"content": part}).encode())
        elif kind == "slack":
            post_raw(ch["webhook_url"], json.dumps({"text": text}).encode())
        else:
            raise ValueError(f"unknown channel type: {kind}")

    def send_webhook(self, ch, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {}
        if ch.get("secret"):
            headers["X-Signature"] = "sha256=" + hmac.new(ch["secret"].encode(), body, hashlib.sha256).hexdigest()
        post_raw(ch["url"], body, headers)

    def send_email(self, ch, subject, body):
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = subject, ch["from"], ", ".join(ch["to"])
        m.set_content(body)
        with smtplib.SMTP(ch["smtp_host"], ch.get("smtp_port", 587), timeout=20) as s:
            s.starttls()
            s.login(ch["username"], ch["password"])
            s.send_message(m)

    def send_test(self):
        results = []
        for ch in self.load():
            kind = ch.get("type")
            try:
                if kind == "webhook":
                    self.send_webhook(ch, {"events": [], "test": True})
                elif kind == "email":
                    self.send_email(ch, "Test: Financial News Aggregator", "Email alerts are working.")
                else:
                    self.send_text(ch, "Test notification from Financial News Aggregator. Alerts are working.")
                results.append({"channel": kind, "ok": True})
            except Exception as ex:
                results.append({"channel": kind, "ok": False, "error": str(ex)[:200]})
        return results or [{"ok": False, "error": "No enabled channels found in notify.json"}]
