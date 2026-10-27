#!/usr/bin/env bash
# Установка и запуск лид-сканера на VPS одной командой:
#   bash deploy.sh
# Скрипт спросит недостающие ключи, сам узнает id вашего чата с ботом,
# войдёт в рабочий Telegram-аккаунт (придёт код) и запустит сервис в Docker.
# Повторный запуск безопасен: заполненное в .env не спрашивается заново.
set -euo pipefail
cd "$(dirname "$0")"
[[ $EUID -eq 0 ]] || { echo "Запустите от root: sudo bash deploy.sh" >&2; exit 1; }

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- защита сервера ------------------------------------------------------------------
harden() {
  if [[ $EUID -ne 0 ]] || ! command -v apt-get >/dev/null 2>&1; then
    echo "Пропускаю настройку защиты сервера (нужны root и Ubuntu/Debian)."
    return
  fi
  if [[ -f /etc/fail2ban/jail.d/leadscanner-sshd.local ]]; then
    return  # уже настроено при прошлом запуске
  fi
  say "Защита сервера: автообновления безопасности, файрвол, защита SSH от перебора паролей"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ufw fail2ban python3-systemd unattended-upgrades >/dev/null
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
    > /etc/apt/apt.conf.d/20auto-upgrades
  printf '[sshd]\nenabled = true\nbackend = systemd\nmaxretry = 5\nfindtime = 10m\nbantime = 1h\n' \
    > /etc/fail2ban/jail.d/leadscanner-sshd.local
  systemctl enable fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban >/dev/null 2>&1 || true
  # Порты, на которых сейчас слушает sshd, — чтобы файрвол не отрезал вам доступ.
  local ports
  ports=$(ss -tlnpH 2>/dev/null | awk '/sshd/ {for (i = 1; i <= NF; i++) if ($i ~ /:[0-9]+$/) {n = split($i, a, ":"); print a[n]; break}}' | sort -u | tr '\n' ' ')
  [[ -n "${ports// /}" ]] || ports=22
  for p in $ports; do ufw allow "${p}/tcp" >/dev/null; done
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw --force enable >/dev/null
  echo "Файрвол включён: снаружи открыт только SSH (порт ${ports% }). Сканеру входящие соединения не нужны."
}
harden

# --- Docker ----------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Docker не найден. Установить его сейчас (нужны права root)? [y/N]"
  read -r answer
  [[ "$answer" =~ ^[YyДд] ]] || fail "Без Docker не запустить. Установите его и повторите: bash deploy.sh"
  curl -fsSL https://get.docker.com | sh
fi
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  fail "Нет docker compose. Установите плагин: apt install docker-compose-plugin"
fi

# --- .env ------------------------------------------------------------------------
[[ -f .env ]] || cp .env.example .env
chmod 600 .env
mkdir -p data
chmod 700 data
# Сервис в контейнере работает не от root, а от пользователя 10001 — ему нужна запись в data/.
chown 10001:10001 data 2>/dev/null || true

get_env() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
set_env() {
  local tmp=".env.tmp.$$"
  (umask 077; grep -vE "^$1=" .env > "$tmp" || true)
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > .env && rm -f "$tmp"
}
ask() {  # ask KEY "подсказка" [secret]
  [[ -n "$(get_env "$1")" ]] && return
  local value=""
  while [[ -z "$value" ]]; do
    if [[ "${3:-}" == secret ]]; then
      read -rsp "$2: " value; echo
    else
      read -rp "$2: " value
    fi
  done
  set_env "$1" "$value"
}

say "Шаг 1/5. Ключи (хранятся только в .env на этом сервере)"
ask TG_API_ID "TG_API_ID (my.telegram.org → API development tools)"
ask TG_API_HASH "TG_API_HASH" secret
ask BOT_TOKEN "Токен бота от @BotFather" secret

if [[ -z "$(get_env LLM_PROVIDER)" ]]; then
  echo
  echo "Какая нейросеть оценивает лидов?"
  echo "  1) Google Gemini — бесплатно (ключ: aistudio.google.com → Get API key)"
  echo "  2) Groq — бесплатно (ключ: console.groq.com → API Keys)"
  echo "  3) OpenRouter — бесплатные модели (ключ: openrouter.ai → Keys)"
  echo "  4) Claude — платно (ключ: console.anthropic.com)"
  read -rp "Номер [1]: " choice
  case "${choice:-1}" in
    2) set_env LLM_PROVIDER groq ;;
    3) set_env LLM_PROVIDER openrouter ;;
    4) set_env LLM_PROVIDER anthropic ;;
    *) set_env LLM_PROVIDER gemini ;;
  esac
fi
PROVIDER=$(get_env LLM_PROVIDER)
case "$PROVIDER" in
  anthropic)
    ask ANTHROPIC_API_KEY "Ключ Claude API" secret ;;
  *)
    ask LLM_API_KEY "Ключ API ($PROVIDER)" secret
    case "$PROVIDER" in
      gemini) MODELS_URL="https://generativelanguage.googleapis.com/v1beta/openai/models" ;;
      groq)   MODELS_URL="https://api.groq.com/openai/v1/models" ;;
      openrouter) MODELS_URL="https://openrouter.ai/api/v1/key" ;;
      *)      MODELS_URL="" ;;
    esac
    if [[ -n "$MODELS_URL" ]]; then
      CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(get_env LLM_API_KEY)" "$MODELS_URL" || true)
      if [[ "$CODE" == 200 ]]; then
        echo "Ключ $PROVIDER работает."
      else
        echo "⚠️  $PROVIDER ответил HTTP $CODE на проверку ключа. Проверьте LLM_API_KEY в .env"
        echo "    (403/400 с сервера в РФ — сервис недоступен из этой страны, нужен VPS за рубежом)."
      fi
    fi ;;
esac

TOKEN=$(get_env BOT_TOKEN)
ME=$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getMe" || true)
[[ "$ME" == *'"ok":true'* ]] || fail "Telegram не принял токен бота. Проверьте BOT_TOKEN в .env"
BOT_NAME=$(printf '%s' "$ME" | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "Бот: @${BOT_NAME}"

# --- id чата оператора -------------------------------------------------------------
if [[ -z "$(get_env OPERATOR_CHAT_ID)" ]]; then
  say "Шаг 2/5. Откройте @${BOT_NAME} в Telegram со СВОЕГО аккаунта и нажмите «Старт» (/start)."
  echo "Жду до 3 минут…"
  CHAT_ID=""
  for _ in $(seq 1 36); do
    UPDATES=$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getUpdates?allowed_updates=%5B%22message%22%5D" || true)
    CHAT_ID=$(printf '%s' "$UPDATES" | grep -o '"chat":{"id":-\?[0-9]*' | tail -1 | grep -o -- '-\?[0-9]*$' || true)
    [[ -n "$CHAT_ID" ]] && break
    sleep 5
  done
  [[ -n "$CHAT_ID" ]] || fail "Не дождался /start. Напишите боту и запустите bash deploy.sh ещё раз."
  set_env OPERATOR_CHAT_ID "$CHAT_ID"
  echo "Чат оператора: $CHAT_ID"
else
  say "Шаг 2/5. Чат оператора уже задан: $(get_env OPERATOR_CHAT_ID)"
fi

# --- сборка ------------------------------------------------------------------------
say "Шаг 3/5. Сборка образа"
"${DC[@]}" build

# --- вход в рабочий аккаунт ------------------------------------------------------------
if [[ -z "$(get_env TG_SESSION)" ]]; then
  say "Шаг 4/5. Вход в РАБОЧИЙ Telegram-аккаунт (не основной!)"
  echo "Введите номер телефона аккаунта, затем код из Telegram (и пароль 2FA, если включён)."
  "${DC[@]}" run --rm -e SESSION_OUT=/app/data/.tg_session leadscanner python -m leadscanner login
  SESSION=$(cat data/.tg_session 2>/dev/null || "${DC[@]}" run --rm -T leadscanner cat /app/data/.tg_session)
  [[ -n "$SESSION" ]] || fail "Сессия не получена — повторите bash deploy.sh"
  set_env TG_SESSION "$SESSION"
  rm -f data/.tg_session 2>/dev/null || "${DC[@]}" run --rm -T leadscanner rm -f /app/data/.tg_session
else
  say "Шаг 4/5. Аккаунт уже авторизован"
fi

# --- запуск ------------------------------------------------------------------------
say "Чаты рабочего аккаунта (id · @username · название):"
"${DC[@]}" run --rm -T leadscanner python -m leadscanner chats || true
echo "Сейчас сканер слушает ВСЕ группы из списка. Чтобы ограничить —"
echo "впишите нужные в config/settings.yaml → telegram.chats (подхватится без перезапуска)."

say "Шаг 5/5. Запуск"
"${DC[@]}" up -d
sleep 5
"${DC[@]}" logs --tail 20 leadscanner
if [[ $EUID -eq 0 ]] && [[ ! -f data/.root_password_changed ]]; then
  say "Пароль root этого сервера пересылался в переписке. Сменить его сейчас? [Y/n]"
  read -r answer
  if [[ ! "$answer" =~ ^[NnНн] ]]; then
    echo "Придумайте длинный пароль (12+ символов) и сохраните его в менеджере паролей."
    passwd root && touch data/.root_password_changed
  fi
fi

say "Готово. В @${BOT_NAME} должно прийти «🟢 Лид-сканер запущен». Команды бота: /help"
echo "Логи: ${DC[*]} logs -f   ·   Стоп: ${DC[*]} down   ·   Обновить: git pull && ${DC[*]} up -d --build"
