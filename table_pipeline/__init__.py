"""
table_pipeline
══════════════════════════════════════════════════════════════════════════
Pipeline isolado para extração de TABELAS de DXF/DWG de PSCIP.

Diferente da abordagem por clusters de texto (api.py), aqui a detecção é
GEOMÉTRICA: as próprias linhas da tabela definem o recorte. Uma keyword
marca o ponto de interesse e o retângulo cresce seguindo a grade conectada
até a borda externa da tabela — sem cortar células no meio.

Fluxo (ver pipeline.run_pipeline + render_tables):
  PASSO 1 — Avalia a qualidade do DXF; se baixa, lança LowQualityDXFError.
  PASSO 2 — Mede a escala do texto (define a resolução do grid).
  PASSO 3 — Detecta tabelas: grid de ocupação → componentes conectados →
            retângulo de cada tabela, pontuado por keywords PSCIP.
  PASSO 4 — Loga qualidade + tabelas no console do uvicorn.
  PASSO 5 — Renderiza as tabelas em ALTA RESOLUÇÃO (legível por LLM).
  PASSO 6 — Empacota em ZIP (no router) ou salva em disco (no script batch).
"""
from .router import router
from .exceptions import (
    TablePipelineError, LowQualityDXFError,
    NoTablesDetectedError, NoRenderableTablesError,
)

__all__ = [
    "router",
    "TablePipelineError", "LowQualityDXFError",
    "NoTablesDetectedError", "NoRenderableTablesError",
]
