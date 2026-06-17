# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────────────
# Imagem Python slim — matplotlib + ezdxf + boto3 (sem GUI; backend Agg).
# Inclui o ODA File Converter (DWG → DXF), que é um app Qt6 e exige um runtime
# X11/XCB completo + display virtual (xvfb) mesmo rodando headless.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg

# Dependências de sistema: matplotlib (fontes) + boto3 e o runtime completo do ODA
RUN apt-get update && apt-get install -y --no-install-recommends \
        # matplotlib / boto3
        libfreetype6 \
        libpng16-16 \
        fonts-dejavu-core \
        ca-certificates \
        # --- ODA File Converter: OpenGL + X11/XCB (Qt6) + display virtual ---
        libgl1 \
        libglu1-mesa \
        libx11-6 \
        libxt6 \
        libxrender1 \
        libxext6 \
        libxcb1 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xinerama0 \
        libxcb-xkb1 \
        libxkbcommon-x11-0 \
        libxkbcommon0 \
        libfontconfig1 \
        libdbus-1-3 \
        xvfb \
        xauth \
    && rm -rf /var/lib/apt/lists/*

# ODA File Converter a partir do .deb bundled em docker/ (DWG → DXF).
# Instalado como root, antes do USER app. dpkg resolve as deps já presentes acima.
COPY docker/ODAFileConverter.deb /tmp/ODAFileConverter.deb
RUN apt-get update \
    && (dpkg -i /tmp/ODAFileConverter.deb || apt-get install -f -y) \
    && rm -f /tmp/ODAFileConverter.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Camada de dependências (cacheada enquanto requirements.txt não muda)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação
COPY api.py dxf_render.py converter.py ./
COPY table_pipeline/ ./table_pipeline/

# Usuário não-root para segurança (xvfb-run -a funciona como não-root)
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# uvicorn com 2 workers (ajustável via UVICORN_WORKERS no compose)
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}"]
