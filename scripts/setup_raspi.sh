#!/usr/bin/env bash
# ==============================================================================
# rysOS: Script de Instalação no Raspberry Pi (100% Sem Sudo / Userland)
# ==============================================================================

set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "🚀 [rysOS] Instalando no diretório: $APP_DIR"
echo "👤 [rysOS] Usuário: $(whoami) (Modo sem sudo)"

cd "$APP_DIR"

# 1. Instalar uv se não estiver instalado no usuário
if ! command -v uv &> /dev/null; then
    echo "📦 Instalando uv em ~/.local/bin..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

# 2. Criar ambiente virtual e instalar dependências
echo "📦 Criando venv e instalando dependências..."
uv venv
source .venv/bin/activate
uv pip install -e .

# 3. Criar diretório de dados local
mkdir -p data

# 4. Configurar Systemd em nível de Usuário (Zero Sudo)
echo "⚙️ Configurando serviço systemd de usuário em ~/.config/systemd/user/..."
mkdir -p "$HOME/.config/systemd/user"

cat <<EOF > "$HOME/.config/systemd/user/rysos.service"
[Unit]
Description=rysOS Executive OS (Gemini AI & Obsidian Agent)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/rysos serve --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10
EnvironmentFile=$APP_DIR/.env

[Install]
WantedBy=default.target
EOF

# 5. Tentar registrar no systemctl --user se disponível
if command -v systemctl &> /dev/null; then
    systemctl --user daemon-reload 2>/dev/null || true
    echo "✅ Serviço registrado em systemctl --user!"
fi

echo ""
echo "========================================================================"
echo "🎉 rysOS instalado com sucesso!"
echo "========================================================================"
echo ""
echo "👉 Opção 1: Iniciar como Serviço 24/7 (systemctl --user):"
echo "   systemctl --user enable --now rysos"
echo "   systemctl --user status rysos"
echo ""
echo "👉 Opção 2: Iniciar em segundo plano com nohup (se preferir):"
echo "   nohup $APP_DIR/.venv/bin/rysos serve --host 127.0.0.1 --port 8000 > rysos.log 2>&1 &"
echo ""
echo "👉 Opção 3: Rodar no terminal diretamente:"
echo "   uv run rysos serve"
echo "========================================================================"
