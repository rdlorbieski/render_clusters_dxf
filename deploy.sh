#!/usr/bin/env bash
# deploy.sh — build e sobe a API dxf-render via docker compose
set -euo pipefail

# ─── Cores ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      DXF Render API — Deploy Script      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo

# ─── Pré-requisitos ───────────────────────────────────────────────────────────
info "Verificando pré-requisitos..."

command -v docker >/dev/null 2>&1 || error "docker não encontrado. Instale o Docker primeiro."

# Suporta tanto 'docker compose' (v2) quanto 'docker-compose' (v1)
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    error "docker compose não encontrado. Instale o Docker Compose v2."
fi

ok "Docker: $(docker --version)"
ok "Compose: $($DC version 2>/dev/null | head -1)"
echo

# ─── Git pull ─────────────────────────────────────────────────────────────────
info "Atualizando código (git pull)..."

command -v git >/dev/null 2>&1 || error "git não encontrado."

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
info "Branch atual: $BRANCH"

git pull origin "$BRANCH" || error "git pull falhou. Verifique conflitos ou acesso ao repositório."

ok "Código atualizado — commit: $(git rev-parse --short HEAD)"
echo

# ─── .env ─────────────────────────────────────────────────────────────────────
info "Verificando arquivo .env..."

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        ok ".env criado a partir de .env.example"
    else
        warn ".env não encontrado — será criado vazio (defaults serão usados)"
        touch .env
    fi
fi

ok ".env encontrado"

# Carrega vars do .env (sem exportar para o shell atual — apenas valida)
# shellcheck disable=SC2034
_ENV_CONTENT=$(grep -v '^\s*#' .env | grep -v '^\s*$' || true)

# Função: lê uma var do .env
_get_env() {
    grep -E "^${1}=" .env | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true
}

# ─── Variáveis opcionais — injeta defaults se ausentes ────────────────────────
info "Verificando variáveis opcionais..."

declare -A DEFAULTS=( [UVICORN_WORKERS]=1 [API_PORT]=8000 )

for var in "${!DEFAULTS[@]}"; do
    val=$(_get_env "$var")
    if [ -z "$val" ]; then
        echo "${var}=${DEFAULTS[$var]}" >> .env
        ok "$var não estava definido — adicionado ao .env com default: ${DEFAULTS[$var]}"
    else
        ok "$var = $val"
    fi
done
echo

# ─── Resumo das configs ────────────────────────────────────────────────────────
API_PORT=$(_get_env "API_PORT"); API_PORT="${API_PORT:-8000}"
WORKERS=$(_get_env "UVICORN_WORKERS"); WORKERS="${WORKERS:-2}"

echo -e "${BOLD}Configuração:${NC}"
echo "  Workers     : $WORKERS"
echo

# ─── Build ────────────────────────────────────────────────────────────────────
info "Buildando imagem Docker..."
$DC build --progress=plain
ok "Build concluído"
echo

# ─── Stop / Start ─────────────────────────────────────────────────────────────
info "Subindo container..."

# Para container antigo se estiver rodando (não falha se não existir)
$DC down --remove-orphans 2>/dev/null || true

$DC up -d
ok "Container iniciado"
echo

# ─── Aguarda healthcheck ───────────────────────────────────────────────────────
info "Aguardando API ficar saudável (até 60s)..."

MAX_WAIT=60
WAITED=0
HEALTHY=0

while [ "$WAITED" -lt "$MAX_WAIT" ]; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' trem-visao 2>/dev/null || echo "unknown")

    if [ "$STATUS" = "healthy" ]; then
        HEALTHY=1
        break
    elif [ "$STATUS" = "unhealthy" ]; then
        break
    fi

    printf "  %ds — status: %s\r" "$WAITED" "$STATUS"
    sleep 3
    WAITED=$((WAITED + 3))
done

echo

if [ "$HEALTHY" -eq 1 ]; then
    ok "API saudável!"
else
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' trem-visao 2>/dev/null || echo "unknown")
    warn "Healthcheck não confirmou healthy em ${MAX_WAIT}s (status atual: $STATUS)"
    warn "Verifique os logs abaixo:"
    echo
    $DC logs --tail=30
    echo
    warn "O container pode ainda estar inicializando. Tente: docker inspect trem-visao"
fi

# ─── Status final ─────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}Status dos containers:${NC}"
$DC ps

echo
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  API disponível em: https://visao.tremprov.site      ║${NC}"
echo -e "${BOLD}║  Docs:  https://visao.tremprov.site/docs             ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo
echo "Logs em tempo real:  $DC logs -f"
echo "Parar a API:         $DC down"
