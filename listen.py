"""Print live news in the terminal.  Usage: python listen.py [stream-url]"""
import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/api/stream"
print("Listening on", url, "(Ctrl+C to stop)")
with urllib.request.urlopen(url) as r:
    ev = "new"
    for raw in r:
        line = raw.decode("utf-8").strip()
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            a = json.loads(line[5:])
            print(f"[{ev.upper()}] {a['source']}: {a['title']}\n   {a['link']}")
