#!/usr/bin/env bash
# Обновление на Linux-сервере одной командой (systemd-юниты из deploy/systemd):
#   bash deploy/update.sh            # обновить и перезапустить
#   bash deploy/update.sh --no-restart
# Делает: git pull --ff-only --autostash -> pip install -r requirements.txt ->
# (если нет opentele) scripts/install_tdata_deps.py -> systemctl restart -> проверка панели.
# Локальные правки отслеживаемых файлов (например, свой server_name в deploy/nginx.conf)
# сохраняются --autostash; .env, data/ и venv вне git и не затрагиваются.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="venv/bin/python"
[ -x "$PY" ] || { echo "Не найден $PY — сначала установка (README.md)"; exit 1; }
NO_RESTART=0
[ "${1:-}" = "--no-restart" ] && NO_RESTART=1

echo "[1/4] git pull..."
old="$(git rev-parse HEAD)"
git pull --ff-only --autostash
new="$(git rev-parse HEAD)"
if [ "$old" = "$new" ]; then
  echo "Уже последняя версия (${new:0:7})."
  [ "${1:-}" = "--force" ] || exit 0
else
  echo "Обновлено: ${old:0:7} -> ${new:0:7}"
  git log --oneline "$old..$new" | head -15 | sed 's/^/  /'
fi

echo "[2/4] зависимости..."
"$PY" -m pip install -q --disable-pip-version-check -r requirements.txt

echo "[3/4] зависимости импорта TData..."
if ! "$PY" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('opentele') else 1)"; then
  "$PY" scripts/install_tdata_deps.py || echo "  Импорт TData пока недоступен (остальное работает)."
fi

if [ "$NO_RESTART" = 1 ]; then echo "[4/4] перезапуск пропущен"; exit 0; fi

echo "[4/4] перезапуск..."
sudo systemctl restart ai-responder-web ai-responder-worker
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/login; then echo "Готово: сайт и воркер обновлены и работают."; exit 0; fi
  sleep 2
done
echo "Панель не ответила за 40 секунд:"
sudo journalctl -u ai-responder-web -n 15 --no-pager || true
exit 1
