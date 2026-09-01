"""
parsed_dwg.py — Etapa 1: parse do DWG independente de regra.

Produz o artefato ParsedDWG (qualidade, escala, textos, segmentos) que as
chamadas de detecção por regra (Etapa 2) consomem sem reabrir o DXF.
Reaproveita collect_text_boxes / collect_segments / avaliar_qualidade /
medir_escala_texto tal como já existem — nenhuma delas foi alterada aqui.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field

from .geometry import collect_segments, collect_text_boxes, TextBox
from .pipeline import avaliar_qualidade, medir_escala_texto
from .exceptions import LowQualityDXFError

_log = logging.getLogger("table_pipeline")
_TEMP_PREFIX = "table_pipeline_parsed_"
_DXF_PREFIX = "table_pipeline_dwg_"


def dxf_path_for_job(job_id: str) -> str:
    """Path convencional do DXF bruto guardado entre /tables/parse e
    /tables/render-final — mesmo padrão de job_id do ParsedDWG. Renderizar
    precisa reabrir o desenho de verdade (vetores completos), não só o
    texto/geometria resumidos que o ParsedDWG guarda."""
    return os.path.join(tempfile.gettempdir(), f"{_DXF_PREFIX}{job_id}.dxf")


def cleanup_job(job_id: str) -> None:
    """Remove o ParsedDWG e o DXF bruto associados a um job_id.

    Limitação conhecida: se ninguém chamar /tables/render-final (o
    consumidor desiste no meio do caminho), esses dois arquivos ficam
    órfãos no /tmp. Política de expiração fica pro backlog."""
    for path in (
        os.path.join(tempfile.gettempdir(), f"{_TEMP_PREFIX}{job_id}.json"),
        dxf_path_for_job(job_id),
    ):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


@dataclass
class ParsedDWG:
    """Resultado da Etapa 1 — tudo que não depende de regra."""

    qualidade: str
    motivo: str
    text_height: float
    cell: float
    gap_cells: int
    text_boxes: list[TextBox] = field(default_factory=list)
    segments: list[tuple[float, float, float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serializa pra JSON — usado pra persistir em arquivo temporário
        entre a Etapa 1 e as chamadas da Etapa 2."""
        return {
            "qualidade": self.qualidade,
            "motivo": self.motivo,
            "text_height": self.text_height,
            "cell": self.cell,
            "gap_cells": self.gap_cells,
            "text_boxes": [
                {
                    "x_min": tb.x_min,
                    "y_min": tb.y_min,
                    "x_max": tb.x_max,
                    "y_max": tb.y_max,
                    "text": tb.text,
                    "height": tb.height,
                }
                for tb in self.text_boxes
            ],
            "segments": [list(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ParsedDWG":
        """Reconstrói a partir do que to_dict produziu."""
        return cls(
            qualidade=data["qualidade"],
            motivo=data["motivo"],
            text_height=data["text_height"],
            cell=data["cell"],
            gap_cells=data["gap_cells"],
            text_boxes=[TextBox(**tb) for tb in data["text_boxes"]],
            segments=[tuple(s) for s in data["segments"]],
        )


def parse_dwg(
    doc,
    *,
    cell_factor: float = 1.0,
    gap_factor: float = 2.5,
) -> ParsedDWG:
    """Etapa 1 — roda uma vez por DWG, antes de qualquer regra.

    Mesma lógica dos PASSOS 1-3 de run_pipeline (qualidade, escala, coleta
    de texto/segmentos) — só que devolve o artefato em vez de já detectar
    tabelas, porque isso passa a ser trabalho da Etapa 2 (uma vez por regra).

    Raises:
        LowQualityDXFError: DXF de qualidade baixa — não compensa seguir.
    """
    msp = doc.modelspace()

    qualidade, motivo = avaliar_qualidade(doc, msp)
    if qualidade == "baixa":
        _log.warning("[table_pipeline] qualidade BAIXA → abortando: %s", motivo)
        raise LowQualityDXFError(qualidade, motivo)

    text_height = medir_escala_texto(msp)
    cell = max(text_height * cell_factor, 1e-6)
    gap_cells = max(int(round(gap_factor / cell_factor)), 1)

    return ParsedDWG(
        qualidade=qualidade,
        motivo=motivo,
        text_height=text_height,
        cell=cell,
        gap_cells=gap_cells,
        text_boxes=collect_text_boxes(msp),
        segments=collect_segments(msp),
    )


def save_parsed_dwg(parsed: ParsedDWG, job_id: str) -> str:
    """Persiste o ParsedDWG em arquivo temporário associado a um job_id.

    Provisório: arquivo temporário mesmo (decisão registrada no plano).
    Migrar pra banco fica pra depois, sem mudar o contrato do endpoint.

    Returns:
        Path do arquivo salvo.
    """
    path = os.path.join(tempfile.gettempdir(), f"{_TEMP_PREFIX}{job_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(parsed.to_dict(), f)
    return path


def load_parsed_dwg(job_id: str) -> ParsedDWG:
    """Carrega o ParsedDWG salvo por save_parsed_dwg.

    Raises:
        FileNotFoundError: job_id não existe ou o arquivo temporário já
            foi limpo pelo sistema — o router traduz isso pra 404.
    """
    path = os.path.join(tempfile.gettempdir(), f"{_TEMP_PREFIX}{job_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return ParsedDWG.from_dict(json.load(f))
