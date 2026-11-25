#!/bin/sh
# One-time server setup: asks a few questions, writes .env, creates ./data, starts everything.
set -e
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Install Docker first: https://docs.docker.com/engine/install/"; exit 1; }
[ -f .env ] && { echo ".env already exists. Delete it to run setup again."; exit 1; }

printf "Domain name pointing at this server (e.g. news.example.com; use 'localhost' to test): "; read DOMAIN
printf "Dashboard username [admin]: "; read AUTH_USER; AUTH_USER=${AUTH_USER:-admin}
printf "Dashboard password: "; stty -echo 2>/dev/null || true; read PW; stty echo 2>/dev/null || true; echo
[ -n "$PW" ] || { echo "Password cannot be empty"; exit 1; }
printf "Website allowed to call the API from a browser (e.g. https://yoursite.com, empty = any): "; read CORS
HASH=$(docker run --rm caddy:2 caddy hash-password --plaintext "$PW")
KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')

mkdir -p data
cat > .env <<EOT
DOMAIN=$DOMAIN
AUTH_USER=$AUTH_USER
AUTH_HASH='$HASH'
API_KEY=$KEY
CORS_ORIGINS=${CORS:-*}
APP_UID=$(id -u)
APP_GID=$(id -g)
EOT
chmod 600 .env
docker compose up -d --build
echo
echo "Running:  https://$DOMAIN   (login: $AUTH_USER)"
echo "API key for your website/scripts (header X-API-Key): $KEY"
echo "Telegram alerts: python3 telegram_setup.py <bot-token> high   then copy notify.json into ./data/"
