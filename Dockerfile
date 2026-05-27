# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────────────
# Imagem Python slim — matplotlib + ezdxf + boto3 rodam sem GUI.
# Backend matplotlib é forçado para 'Agg' dentro do api.py, então não
# precisamos de libs X11/Tk no container.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg

# Dependências de sistema para matplotlib (fontes) e libs do boto3
RUN apt-get update && apt-get install -y --no-install-recommends \
        libfreetype6 \
        libpng16-16 \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Camada de dependências (cacheada enquanto requirements.txt não muda)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação
COPY api.py dxf_render.py cloudflare_r2.py ./

# Usuário não-root para segurança
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# uvicorn com 2 workers (ajustável via UVICORN_WORKERS no compose)
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}"]
