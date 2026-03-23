#!/usr/bin/env bash
# EC2 bootstrap script — run once as root on a fresh Ubuntu 22.04 instance.
set -euo pipefail

APP_DIR="/opt/funder-monitor"
REPO_URL="https://github.com/joachimmpanda97/ubongo-funder-monitor.git"

echo "==> Updating system packages"
apt-get update && apt-get upgrade -y

echo "==> Installing Python, PostgreSQL, and utilities"
apt-get install -y python3 python3-venv python3-pip \
    postgresql postgresql-contrib git curl

echo "==> Setting up PostgreSQL"
systemctl enable postgresql
systemctl start postgresql
# Create DB user and database (idempotent)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='funder_user'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER funder_user WITH PASSWORD 'changeme';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='funder_monitor'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE funder_monitor OWNER funder_user;"

echo "==> Cloning application"
git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

echo "==> Creating Python virtual environment"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing Playwright browser"
python -m playwright install chromium
python -m playwright install-deps chromium

echo "==> Copying .env (edit this before running the app)"
cp .env.example .env
echo "  --> Edit $APP_DIR/.env with your real credentials before proceeding."

echo "==> Installing cron job"
crontab deploy/crontab.txt

echo ""
echo "Setup complete. Next steps:"
echo "  1. Edit $APP_DIR/.env"
echo "  2. Run: cd $APP_DIR && source venv/bin/activate && python -m db.init_db"
echo "  3. Run: python -m scraper.directory_scraper   (one-time funder import)"
echo "  4. Verify cron: crontab -l"
