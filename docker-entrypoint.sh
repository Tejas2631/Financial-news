#!/bin/sh
set -e
if ! touch /data/.write-test 2>/dev/null; then
  echo "ERROR: /data is not writable by uid $(id -u). On the host run:  mkdir -p data && sudo chown $(id -u):$(id -g) data" >&2
  echo "       or set APP_UID / APP_GID in .env to the owner of ./data" >&2
  exit 1
fi
rm -f /data/.write-test
for f in feeds.json watchlist.json; do
  [ -f "/data/$f" ] || cp "/app/defaults/$f" "/data/$f"
done
exec "$@"
