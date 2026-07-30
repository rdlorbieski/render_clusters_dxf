"""
exceptions.py — exceções de domínio do pipeline de extração de tabelas.

Modelam as condições em que o pipeline não produz tabelas extraíveis. Cada
uma carrega `qualidade` e `motivo` para que o consumidor (endpoint ou script)
traduza em uma resposta amigável sem inspecionar flags internos.
"""
from __future__ import annotations


class TablePipelineError(Exception):
    """Base de todas as exceções do pipeline de tabelas."""

    def __init__(self, qualidade: str, motivo: str):
        self.qualidade = qualidade
        self.motivo = motivo
        super().__init__(motivo)


class LowQualityDXFError(TablePipelineError):
    """O DXF é de qualidade baixa — a extração de tabelas não compensa.

    Lançada no PASSO 1. O `motivo` explica o porquê (paper-space, poucos
    textos, planta muito alongada, etc.).
    """


class NoTablesDetectedError(TablePipelineError):
    """Nenhuma região com grade + keywords PSCIP foi localizada (PASSO 3)."""

    def __init__(self, qualidade: str,
                 motivo: str = "Nenhuma tabela com keywords PSCIP foi localizada."):
        super().__init__(qualidade, motivo)


class NoRenderableTablesError(TablePipelineError):
    """Tabelas detectadas, mas nenhuma pôde ser renderizada (PASSO 5)."""

    def __init__(self, qualidade: str,
                 motivo: str = "Tabelas detectadas, mas nenhuma pôde ser renderizada."):
        super().__init__(qualidade, motivo)
