FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# O código precisa existir antes do install editável: o pacote resolve a raiz do
# projeto (.env, data/, tokens/, credentials.json) a partir de onde está instalado.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv pip install --system -e . && mkdir -p /app/data /app/tokens

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Dentro do contêiner o servidor escuta em todas as interfaces; quem publica a porta
# para fora é o docker-compose.yml (por padrão, só em 127.0.0.1 do host).
CMD ["rysos", "serve", "--host", "0.0.0.0", "--port", "8000"]
