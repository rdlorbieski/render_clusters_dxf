"""
table_pipeline
══════════════════════════════════════════════════════════════════════════
Pipeline isolado para extração de TABELAS de DXF/DWG de PSCIP.

Diferente da abordagem por clusters de texto (api.py), aqui a detecção é
GEOMÉTRICA: as próprias linhas da tabela definem o recorte. Uma keyword
marca o ponto de interesse e o retângulo cresce seguindo a grade conectada
até a borda externa da tabela — sem cortar células no meio.

Fluxo (6 passos, ver pipeline.run_pipeline):
  1. Avalia qualidade do DXF; se baixa, aborta com o motivo.
  2. Detecta regiões com grade (tabelas) em baixo custo via grid de ocupação.
  3. Extrai o retângulo (xmin,ymin,xmax,ymax) de cada tabela.
  4. Pontua cada tabela pela presença de keywords PSCIP.
  5. Loga qualidade + tabelas no console do uvicorn.
  6. Renderiza as tabelas de maior score em ALTA RESOLUÇÃO (legível por LLM)
     e devolve um ZIP.
"""
from .router import router

__all__ = ["router"]
