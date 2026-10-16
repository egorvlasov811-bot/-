#!/bin/sh
# Деплой footy на сервер: ./deploy/deploy.sh [ssh-хост]   (деф. tgpanel-backup)
# Код -> /opt/footy/app, venv -> /opt/footy/venv, сервисы footy-bot/footy-web.
# .env копируется с правами 600 (ключи в git не попадают).
set -eu
HOST="${1:-tgpanel-backup}"
cd "$(dirname "$0")/.."

ssh "$HOST" 'set -e
  command -v rsync >/dev/null && python3 -c "import ensurepip" 2>/dev/null || {
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive \
      apt-get install -y -qq rsync python3-venv >/dev/null; }
  id footy >/dev/null 2>&1 || useradd --system --home /opt/footy --shell /usr/sbin/nologin footy
  mkdir -p /opt/footy/app'

rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.footy_cache' --exclude 'hist' --exclude '.env' --exclude '*.db' \
  --exclude '.footy_profile.json' \
  ./ "$HOST:/opt/footy/app/"
scp -q .env "$HOST:/opt/footy/app/.env"
scp -q deploy/*.service deploy/*.timer "$HOST:/etc/systemd/system/"

ssh "$HOST" 'set -e
  [ -x /opt/footy/venv/bin/pip ] || { rm -rf /opt/footy/venv; python3 -m venv /opt/footy/venv; }
  /opt/footy/venv/bin/pip install -q -r /opt/footy/app/requirements.txt
  chown -R footy:footy /opt/footy
  chmod 600 /opt/footy/app/.env
  systemctl daemon-reload
  systemctl enable -q footy-bot footy-web
  systemctl restart footy-bot footy-web
  # утренний снимок журнала теперь делает footy-digest (те же кэфы)
  if [ -f /etc/systemd/system/footy-journal.timer ]; then
    systemctl disable -q --now footy-journal.timer || true
    rm -f /etc/systemd/system/footy-journal.timer /etc/systemd/system/footy-journal.service
    systemctl daemon-reload
  fi
  systemctl enable -q --now footy-digest.timer footy-journal-cl.timer
  sleep 2
  systemctl --no-pager --lines=0 status footy-bot footy-web | grep -E "●|Active"'
echo "Готово. Веб: ssh -L 8050:127.0.0.1:8050 $HOST  ->  http://127.0.0.1:8050"
