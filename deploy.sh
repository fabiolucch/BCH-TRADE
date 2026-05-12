#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/BCH-TRADE}"
BRANCH="${1:-$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)}"
PY="$REPO_DIR/venv/bin/python"

cd "$REPO_DIR"
echo "[1/8] Repo: $REPO_DIR | Branch: $BRANCH"

if [ -f .env ]; then
  cp .env ".env.bak_$(date +%F_%H-%M-%S)"
  echo "[2/8] Backup .env criado"
else
  echo "[2/8] AVISO: .env não encontrado em $REPO_DIR"
fi

git fetch --all --prune
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "[3/8] Código sincronizado com origin/$BRANCH"

"$PY" -m pip install -U pip
"$PY" -m pip install -r requirements.txt
echo "[4/8] Dependências instaladas no venv do serviço"

"$PY" -m py_compile main.py config.py execution.py indicators.py notifier.py
"$PY" -c "import config, execution, indicators, notifier, main; print('OK imports')"
echo "[5/8] Validação Python OK"

if [ -f /etc/systemd/system/bch-trade.service ]; then
  systemctl daemon-reload || true
fi
systemctl restart bch-trade
sleep 2

echo "[6/8] Status serviço"
systemctl status bch-trade --no-pager -l | sed -n '1,40p'

echo "[7/8] Últimos logs"
journalctl -u bch-trade -n 80 --no-pager

echo "[8/8] Deploy concluído com sucesso"
