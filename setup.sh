#!/usr/bin/env bash
# setup.sh — Instalação do BCH-TRADE Bot em Ubuntu 22.04 / 24.04
# Uso: bash setup.sh
set -euo pipefail

REPO_URL="https://github.com/fabiolucch/bch-trade.git"
BRANCH="claude/new-session-JNgPX"
INSTALL_DIR="$HOME/BCH-TRADE"
SERVICE_NAME="bch-trade"
PYTHON="python3.12"

echo ""
echo "════════════════════════════════════════════════════"
echo "  BCH-TRADE Bot — Setup Automático"
echo "════════════════════════════════════════════════════"
echo ""

# ── 1. Dependências do sistema ────────────────────────
echo "[1/6] Atualizando sistema e instalando Python 3.12..."
sudo apt-get update -qq
sudo apt-get install -y -qq software-properties-common git
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -qq
sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev

# ── 2. Clonar / atualizar repositório ────────────────
echo "[2/6] Clonando repositório..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  → Diretório já existe, atualizando..."
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull origin "$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 3. Ambiente virtual + dependências Python ────────
echo "[3/6] Criando ambiente virtual e instalando dependências..."
$PYTHON -m venv venv
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install -r requirements.txt --quiet

# ── 4. Arquivo .env ───────────────────────────────────
echo "[4/6] Configurando .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  ┌─────────────────────────────────────────────┐"
    echo "  │  ATENÇÃO: .env criado a partir do exemplo.  │"
    echo "  │  Edite com suas credenciais reais:          │"
    echo "  │  nano $INSTALL_DIR/.env                     │"
    echo "  └─────────────────────────────────────────────┘"
    echo ""
else
    echo "  → .env já existe, mantendo configuração atual."
fi

# ── 5. Instalar serviço systemd ───────────────────────
echo "[5/6] Instalando serviço systemd..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo cp "$INSTALL_DIR/bch-trade.service" "$SERVICE_FILE"
sudo sed -i "s|__USER__|$(whoami)|g"    "$SERVICE_FILE"
sudo sed -i "s|__WORKDIR__|$INSTALL_DIR|g" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# ── 6. Resumo ─────────────────────────────────────────
echo "[6/6] Setup concluído!"
echo ""
echo "════════════════════════════════════════════════════"
echo "  PRÓXIMOS PASSOS"
echo "════════════════════════════════════════════════════"
echo ""
echo "  1. Edite o .env com suas credenciais reais:"
echo "     nano $INSTALL_DIR/.env"
echo ""
echo "  2. Certifique-se de que TESTNET=False no .env"
echo "     (conta real OKX El Salvador)"
echo ""
echo "  3. Inicie o bot:"
echo "     sudo systemctl start $SERVICE_NAME"
echo ""
echo "  4. Acompanhe os logs em tempo real:"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  5. Outros comandos úteis:"
echo "     sudo systemctl status  $SERVICE_NAME   # status"
echo "     sudo systemctl stop    $SERVICE_NAME   # parar"
echo "     sudo systemctl restart $SERVICE_NAME   # reiniciar"
echo ""
