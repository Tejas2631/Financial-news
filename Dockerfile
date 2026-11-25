FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py dashboard.html docker-entrypoint.sh ./
# first-run defaults; copied into /data only if missing there
COPY feeds.json watchlist.json ./defaults/
RUN chmod +x docker-entrypoint.sh && mkdir /data
ENV NEWS_DB=/data/news.db FEEDS_FILE=/data/feeds.json WATCHLIST_FILE=/data/watchlist.json \
    NOTIFY_CONFIG=/data/notify.json BACKUP_DIR=/data/backups
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
