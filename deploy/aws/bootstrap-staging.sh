#!/usr/bin/env bash
set -euo pipefail

REGION=us-east-1
DB_HOST=tracelab-mvp-db.c8x4cycgy91u.us-east-1.rds.amazonaws.com
MASTER_SECRET_ARN='arn:aws:secretsmanager:us-east-1:958124171224:secret:rds!db-01d69810-3d27-4ba0-9a08-e7d6215f9453-N8ir44'
APP_SECRET_NAME=tracelab-mvp-app-env

dnf install -y postgresql15
install -d -m 0755 /usr/local/lib/docker/cli-plugins
curl --fail --location --silent --show-error \
  https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64 \
  --output /usr/local/lib/docker/cli-plugins/docker-compose
chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version

if aws secretsmanager describe-secret --secret-id "$APP_SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo 'Application secret already exists; skipping role creation.'
  exit 0
fi

MASTER_PASSWORD="$(aws secretsmanager get-secret-value --secret-id "$MASTER_SECRET_ARN" --region "$REGION" --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
APP_PASSWORD="$(openssl rand -hex 32)"
export PGPASSWORD="$MASTER_PASSWORD"
if [[ "$(psql "host=$DB_HOST port=5432 dbname=tracelab user=tracelab_admin sslmode=require" -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'tracelab_app'")" == 1 ]]; then
  ROLE_SQL="ALTER ROLE tracelab_app LOGIN PASSWORD '$APP_PASSWORD';"
else
  ROLE_SQL="CREATE ROLE tracelab_app LOGIN PASSWORD '$APP_PASSWORD';"
fi
psql "host=$DB_HOST port=5432 dbname=tracelab user=tracelab_admin sslmode=require" -v ON_ERROR_STOP=1 <<SQL
$ROLE_SQL
GRANT CONNECT ON DATABASE tracelab TO tracelab_app;
GRANT USAGE, CREATE ON SCHEMA public TO tracelab_app;
SQL
unset MASTER_PASSWORD PGPASSWORD

install -d -m 0700 /srv/tracelab/deploy
umask 077
printf 'DATABASE_URL=postgresql+asyncpg://tracelab_app:%s@%s:5432/tracelab\n' "$APP_PASSWORD" "$DB_HOST" > /srv/tracelab/deploy/app.env
aws secretsmanager create-secret --name "$APP_SECRET_NAME" --region "$REGION" \
  --secret-string file:///srv/tracelab/deploy/app.env \
  --tags Key=Project,Value=TraceLab >/dev/null
unset APP_PASSWORD
echo 'Application database role and secret created.'
