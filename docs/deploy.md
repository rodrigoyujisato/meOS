# Deploy

O rysOS é um processo único (FastAPI + agendador + bot do Telegram) que precisa de uma
pasta de cofre e de um `.env`. Três formas de rodar, da mais simples à mais robusta.

Antes de qualquer uma: siga a instalação e a configuração das credenciais do
[README](../README.md).

## 1. Direto no terminal

```bash
uv sync
uv run rysos serve
```

## 2. Serviço systemd de usuário (Linux, sem sudo)

Serve para um mini servidor caseiro ou um Raspberry Pi.

```bash
./scripts/setup_raspi.sh        # cria .venv, instala e registra ~/.config/systemd/user/rysos.service
systemctl --user enable --now rysos
journalctl --user -u rysos -f
loginctl enable-linger "$USER"  # mantém o serviço no ar sem sessão aberta
```

Preferindo escrever a unidade à mão, use `docs/deploy/rysos.service.example`. Se o cofre
estiver no Google Drive via rclone, `docs/deploy/gdrive-mount.service.example` monta o Drive
e o `rysos.service` passa a depender dele.

O comando `systemctl --user` precisa de `XDG_RUNTIME_DIR=/run/user/$(id -u)` numa sessão
SSH sem login gráfico.

## 3. Docker

```bash
cp .env.example .env            # preencha
mkdir -p vault data tokens      # cofre, banco e tokens ficam fora da imagem
# credentials.json e tokens/: gerados por `rysos auth` na sua máquina (o OAuth abre um navegador)
docker compose up -d --build
```

O compose publica a porta só em `127.0.0.1`. A interface web não tem autenticação:
para acessar de outro dispositivo, coloque uma VPN (por exemplo Tailscale) ou um túnel com
controle de acesso na frente. Nunca exponha a porta direto na internet.

## Atualizar

```bash
git pull
uv sync
systemctl --user restart rysos   # ou: docker compose up -d --build
```

O banco SQLite (`data/rysos.db`) é criado no primeiro boot. Faça backup dessa pasta e do
cofre; o restante é reconstruível.
