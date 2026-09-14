"""
regra.py — Etapa 2 e 3: detecção e render de tabelas por regra/sistema.

Etapa 2 (detect_tables_regra) roda a partir de um ParsedDWG (Etapa 1) — não
reabre o DXF, não recoleta texto/segmentos. Reaproveita o núcleo geométrico
de detect_tables (_detect_tables_core, em pipeline.py); só troca a lista de
keywords e desliga os bônus genéricos de RT/classificação de ocupação/norma
técnica: esses fazem sentido pra achar "alguma tabela relevante" no fluxo
atual, não pra decidir se uma tabela pertence a UM sistema específico.

Etapa 3 (aggregate_and_render) roda depois que TODAS as regras terminaram
a Etapa 2 — reabre o DXF (agora sim precisa dos vetores completos) e
reaproveita render_tables (pipeline.py) sem alterar nada nela: só
deduplica bbox repetido entre regras antes de montar a lista que vai pra
render_tables, pra não desenhar a mesma tabela duas vezes.
"""

from __future__ import annotations

from .parsed_dwg import ParsedDWG
from .pipeline import (
    PipelineResult,
    Table,
    _detect_tables_core,
    _select_diverse,
    render_tables,
)


def detect_tables_regra(
    parsed: ParsedDWG,
    keywords: list[str],
    *,
    n: int = 3,
    min_keywords: int = 1,
    roi_margin_factor: float = 60.0,
    group_factor: float = 25.0,
) -> list[Table]:
    """Etapa 2 — tabelas de UM sistema/regra, a partir do ParsedDWG da Etapa 1.

    Args:
        parsed: saída de parse_dwg (Etapa 1).
        keywords: termos desse sistema/regra (ex.: palavras_chave_sistema
            do Hyego para EXTINTORES: ["EXTINTOR", "EXTINTORES"]).
        n: teto de imagens pra esse sistema — não é meta; _select_diverse
            já para antes se cobrir tudo com menos.
        min_keywords: piso de batidas pra contar como tabela real. Default
            1 (não 2, como no fluxo genérico) porque vocabulário por
            sistema é estreito — uma tabela de extintor pode citar a
            palavra uma vez só e descrever o resto em termos técnicos que
            não repetem "extintor". Testado: com min_keywords=2 nessa
            situação a tabela some inteira, falso negativo real.

    Returns:
        Tabelas desse sistema, já deduplicadas por _select_diverse.
    """
    tables = _detect_tables_core(
        parsed.text_boxes,
        parsed.segments,
        parsed.text_height,
        parsed.cell,
        parsed.gap_cells,
        keywords=keywords,
        include_generic_bonuses=False,
        min_keywords=min_keywords,
        roi_margin_factor=roi_margin_factor,
        group_factor=group_factor,
    )
    return _select_diverse(tables, n)


def aggregate_and_render(
    doc,
    resultados_por_regra: dict[str, list[Table]],
    *,
    qualidade: str,
    motivo: str,
    text_height: float,
    cell: float,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Etapa 3 — renderiza cada bbox distinto uma vez só, mesmo que
    apareça em mais de uma regra.

    bbox é determinístico: duas regras que acham a MESMA tabela física, a
    partir do mesmo ParsedDWG (mesmos text_boxes/segments/cell/gap_cells),
    produzem o bbox exatamente igual — dedup por igualdade de tupla é
    suficiente, não precisa de IoU aqui.

    Args:
        doc: documento DXF já reaberto (render precisa dos vetores
            completos, não só do ParsedDWG).
        resultados_por_regra: regra -> tabelas que detect_tables_regra
            achou pra ela.
        qualidade, motivo, text_height, cell: do ParsedDWG da Etapa 1 —
            render_tables usa text_height/cell como fallback por tabela.

    Returns:
        (rendered, manifest_por_regra)
        rendered: mesmo shape de render_tables — uma entrada por bbox
            ÚNICO (já deduplicado).
        manifest_por_regra: regra -> lista de nomes de arquivo, repetindo
            o nome quando o bbox é compartilhado entre regras.
    """
    tabelas_unicas: dict[tuple, Table] = {}
    regra_para_bboxes: dict[str, list[tuple]] = {}
    for regra, tabelas in resultados_por_regra.items():
        regra_para_bboxes[regra] = [t.bbox for t in tabelas]
        for t in tabelas:
            tabelas_unicas.setdefault(t.bbox, t)

    fake_result = PipelineResult(
        qualidade=qualidade,
        motivo=motivo,
        tables=list(tabelas_unicas.values()),
        text_height=text_height,
        cell=cell,
        gap_cells=0,  # render_tables não lê gap_cells
    )
    rendered = render_tables(doc, fake_result)

    bbox_para_nome = {r["bbox"]: r["name"] for r in rendered}
    manifest_por_regra = {
        regra: [bbox_para_nome[b] for b in bboxes if b in bbox_para_nome]
        for regra, bboxes in regra_para_bboxes.items()
    }
    return rendered, manifest_por_regra
