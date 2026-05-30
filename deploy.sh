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
        warn ".env não encontrado — copiando de .env.example"
        cp .env.example .env
        warn "Edite .env com suas credenciais reais antes de continuar."
        echo
        cat .env
        echo
        error "Abortando. Configure o .env e rode o deploy novamente."
    else
        error ".env e .env.example não encontrados. Crie o arquivo .env com as variáveis necessárias."
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

# ─── Validação de variáveis obrigatórias ──────────────────────────────────────
info "Validando variáveis de ambiente..."

REQUIRED_VARS=("R2_ACCOUNT_ID" "R2_ACCESS_KEY_ID" "R2_SECRET_ACCESS_KEY" "R2_BUCKET")
PLACEHOLDER_PATTERNS=("your_account_id_here" "your_access_key_here" "your_secret_key_here")

HAS_ERROR=0
for var in "${REQUIRED_VARS[@]}"; do
    val=$(_get_env "$var")
    if [ -z "$val" ]; then
        warn "Variável obrigatória não definida: ${BOLD}$var${NC}"
        HAS_ERROR=1
    else
        # Verifica se ainda é valor de placeholder
        for placeholder in "${PLACEHOLDER_PATTERNS[@]}"; do
            if [ "$val" = "$placeholder" ]; then
                warn "${BOLD}$var${NC} ainda usa valor de exemplo: '$placeholder'"
                HAS_ERROR=1
            fi
        done
    fi
done

# Vars opcionais — só avisa se ausentes
OPTIONAL_VARS=("R2_BASE_URL" "UVICORN_WORKERS" "API_PORT")
for var in "${OPTIONAL_VARS[@]}"; do
    val=$(_get_env "$var")
    if [ -z "$val" ]; then
        warn "Variável opcional não definida (usando default): $var"
    else
        ok "$var = $val"
    fi
done

[ "$HAS_ERROR" -eq 1 ] && error "Corrija as variáveis acima no .env antes de continuar."
ok "Todas as variáveis obrigatórias estão definidas"
echo

# ─── Resumo das configs ────────────────────────────────────────────────────────
API_PORT=$(_get_env "API_PORT"); API_PORT="${API_PORT:-8000}"
WORKERS=$(_get_env "UVICORN_WORKERS"); WORKERS="${WORKERS:-2}"
BUCKET=$(_get_env "R2_BUCKET"); BUCKET="${BUCKET:-trem}"

echo -e "${BOLD}Configuração:${NC}"
echo "  Porta API   : $API_PORT"
echo "  Workers     : $WORKERS"
echo "  Bucket R2   : $BUCKET"
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
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' dxf-render-api 2>/dev/null || echo "unknown")

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
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' dxf-render-api 2>/dev/null || echo "unknown")
    warn "Healthcheck não confirmou healthy em ${MAX_WAIT}s (status atual: $STATUS)"
    warn "Verifique os logs abaixo:"
    echo
    $DC logs --tail=30
    echo
    warn "O container pode ainda estar inicializando. Tente: docker inspect dxf-render-api"
fi

# ─── Status final ─────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}Status dos containers:${NC}"
$DC ps

echo
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  API disponível em: http://localhost:${API_PORT}  ║${NC}"
echo -e "${BOLD}║  Docs:  http://localhost:${API_PORT}/docs        ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo
echo "Logs em tempo real:  $DC logs -f"
echo "Parar a API:         $DC down"
