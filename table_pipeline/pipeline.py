"""
pipeline.py — orquestração da extração de tabelas PSCIP.

Fluxo (ver run_pipeline + render_tables):
    PASSO 1 — Avaliar a qualidade do DXF        (aborta se baixa)
    PASSO 2 — Medir a escala do texto           (resolução do grid)
    PASSO 3 — Detectar tabelas                  (grade conectada + keywords)
    PASSO 4 — Registrar o resultado no log
    PASSO 5 — Renderizar em alta resolução      (render_tables)
    PASSO 6 — Empacotar (ZIP / salvar)          (no router/script)

Reusa funções de api.py e dxf_render.py via import tardio (dentro das
funções) para evitar import circular — o router deste módulo é incluído
no app de api.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .geometry import (
    collect_segments,
    collect_text_boxes,
    TextBox,
    build_occupancy,
    find_components,
    component_bbox,
)
from .exceptions import LowQualityDXFError

_log = logging.getLogger("table_pipeline")


# ─────────────────────────────────────────────────────────────────────────────
# Estruturas de resultado
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Table:
    """Uma tabela detectada."""

    bbox: tuple[float, float, float, float]  # (xmin,ymin,xmax,ymax)
    score: float
    keyword_count: int
    text_count: int
    keywords: list[str] = field(default_factory=list)
    text_height: float = 0.0  # altura do texto de corpo DESTA tabela (p/ DPI)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class PipelineResult:
    """Resultado de uma detecção bem-sucedida (passos 1–4).

    Qualidade baixa não produz um PipelineResult — lança LowQualityDXFError.
    `tables` pode vir vazia se nenhuma região tiver keywords PSCIP.
    """

    qualidade: str
    motivo: str
    tables: list[Table] = field(default_factory=list)
    text_height: float = 0.0
    cell: float = 0.0
    gap_cells: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# PASSO 3 — Detecção geométrica de tabelas (grade conectada + keywords)
# ─────────────────────────────────────────────────────────────────────────────

# Mínimo de textos com keyword para uma região contar como tabela.
# Descarta falsos positivos de keyword isolada (ex.: "ÁREA VERDE" solto gera
# um componente minúsculo que não é uma tabela real).
_MIN_KEYWORDS_PER_TABLE = 2


def _seed_keywords(
    text_boxes: list[TextBox],
    keywords: list[str] | None = None,
    *,
    include_generic_bonuses: bool = True,
) -> list[tuple[TextBox, float]]:
    """Retorna (TextBox, score) das caixas que contêm keyword PSCIP.

    `keywords=None` usa a lista padrão de `_text_score` (_KEYWORDS_HIGH_VALUE)."""
    from api import _text_score

    kwargs = {} if keywords is None else {"keywords": keywords}
    seeds = []
    for tb in text_boxes:
        s = _text_score(
            tb.text, include_generic_bonuses=include_generic_bonuses, **kwargs
        )
        if s > 1.0:  # > 1 significa que bateu alguma keyword
            seeds.append((tb, s))
    return seeds


def _group_seeds(seeds, eps: float):
    """Agrupa seeds próximas (DBSCAN) → vizinhanças a processar separadamente.

    Cada vizinhança vira uma ROI/grid local de alta resolução, evitando um
    grid gigante de baixa resolução para a prancha inteira.
    """
    if not seeds:
        return []
    try:
        from sklearn.cluster import DBSCAN
        import numpy as np

        coords = np.array([(tb.cx, tb.cy) for tb, _ in seeds])
        labels = DBSCAN(eps=eps, min_samples=1).fit_predict(coords)
        groups: dict[int, list] = {}
        for item, lbl in zip(seeds, labels):
            groups.setdefault(int(lbl), []).append(item)
        return list(groups.values())
    except ImportError:
        return [seeds]  # tudo num grupo só


def _detect_tables_core(
    text_boxes: list[TextBox],
    segments: list[tuple[float, float, float, float]],
    text_height: float,
    cell: float,
    gap_cells: int,
    *,
    keywords: list[str] | None = None,
    include_generic_bonuses: bool = True,
    min_keywords: int = _MIN_KEYWORDS_PER_TABLE,
    roi_margin_factor: float = 60.0,
    group_factor: float = 25.0,
) -> list[Table]:
    """Núcleo geométrico compartilhado (grade conectada ancorada em keywords).

    Não decide `cell`/`gap_cells` nem coleta texto/segmentos — quem chama
    já traz isso pronto: `detect_tables` (fluxo atual, a partir de `msp`)
    ou `detect_tables_regra` (fluxo por regra/sistema, a partir de um
    `ParsedDWG` já parseado — ver `regra.py`).

    `min_keywords` default é `_MIN_KEYWORDS_PER_TABLE` (2) — calibrado pra
    lista genérica de ~37 termos, onde 2 batidas distintas é sinal razoável
    de tabela real. Com uma lista estreita por sistema (ex.: só "EXTINTOR"
    e "EXTINTORES"), exigir 2 é bar alto demais: uma tabela real de
    extintor pode mencionar a palavra uma única vez e descrever o resto em
    termos técnicos (carga, tipo, capacidade) que não repetem "extintor".
    `detect_tables_regra` chama com `min_keywords=1` por esse motivo.
    """
    seeds = _seed_keywords(
        text_boxes, keywords, include_generic_bonuses=include_generic_bonuses
    )
    if not seeds:
        return []

    roi_margin = text_height * roi_margin_factor
    group_eps = text_height * group_factor
    groups = _group_seeds(seeds, group_eps)

    tables: list[Table] = []
    seen_bboxes: list[tuple[float, float, float, float]] = []

    for group in groups:
        # ROI = bbox das keywords do grupo + margem generosa (cabe a tabela)
        gx = [tb.cx for tb, _ in group]
        gy = [tb.cy for tb, _ in group]
        region = (
            min(gx) - roi_margin,
            min(gy) - roi_margin,
            max(gx) + roi_margin,
            max(gy) + roi_margin,
        )

        occ = build_occupancy(segments, text_boxes, region, cell)
        labels = find_components(occ, gap_cells=gap_cells)

        # Para cada keyword do grupo, descobre o componente (tabela) dela
        group_labels: set[int] = set()
        for tb, _ in group:
            r, c = occ.world_to_cell(tb.cx, tb.cy)
            H, W = occ.shape
            if 0 <= r < H and 0 <= c < W and labels[r, c] != 0:
                group_labels.add(int(labels[r, c]))

        for lbl in group_labels:
            bbox = component_bbox(occ, labels, lbl, use_original=True)
            if bbox is None:
                continue
            if _is_duplicate(bbox, seen_bboxes):
                continue
            seen_bboxes.append(bbox)

            # Pontua a tabela: soma score de TODOS os textos dentro do bbox
            table = _score_table(
                bbox,
                text_boxes,
                keywords,
                include_generic_bonuses=include_generic_bonuses,
            )
            # Descarta keyword isolada (provável falso positivo)
            if table.keyword_count < min_keywords:
                continue
            tables.append(table)

    tables.sort(key=lambda t: t.score, reverse=True)
    return tables


def detect_tables(
    msp,
    text_height: float,
    *,
    cell_factor: float = 1.0,
    gap_factor: float = 2.5,
    roi_margin_factor: float = 60.0,
    group_factor: float = 25.0,
) -> tuple[list[Table], float, int]:
    """Detecta tabelas via grade conectada ancorada em keywords.

    Args:
        msp: modelspace.
        text_height: altura típica do texto (unidades DXF) — escala base.
        cell_factor: tamanho da célula do grid = text_height × cell_factor.
        gap_factor: gap de dilatação = text_height × gap_factor (fecha vãos
            internos da tabela; menor evita fundir tabelas vizinhas).
        roi_margin_factor: margem da ROI ao redor das keywords (× text_height).
        group_factor: raio p/ agrupar keywords em vizinhanças (× text_height).

    Returns:
        (tables, cell, gap_cells).
    """
    text_boxes = collect_text_boxes(msp)
    segments = collect_segments(msp)
    cell = max(text_height * cell_factor, 1e-6)
    gap_cells = max(int(round(gap_factor / cell_factor)), 1)

    tables = _detect_tables_core(
        text_boxes,
        segments,
        text_height,
        cell,
        gap_cells,
        roi_margin_factor=roi_margin_factor,
        group_factor=group_factor,
    )
    return tables, cell, gap_cells


def _is_duplicate(bbox, seen, iou_thresh: float = 0.6) -> bool:
    """True se bbox sobrepõe fortemente algum já visto (mesma tabela)."""
    for s in seen:
        if _iou(bbox, s) > iou_thresh:
            return True
    return False


def _iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(ix1 - ix0, 0), max(iy1 - iy0, 0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    return inter / (area_a + area_b - inter)


def _select_diverse(tables: list[Table], n: int) -> list[Table]:
    """Escolhe até `n` tabelas priorizando cobertura de keywords, não só score.

    Pegar sempre as `n` de maior score deixa passar o caso em que várias das
    melhores são a MESMA legenda repetida em pranchas diferentes (mesmas
    keywords, bboxes bem distantes — o IoU de `_is_duplicate` não pega isso).
    Aí sobram vagas ocupadas por 4 cópias da mesma tabela e nenhuma para as
    que cobrem keywords que só aparecem nelas.

    Em vez de ordenar e cortar, escolhe a cada passo quem adiciona mais
    keywords NOVAS (ainda não cobertas pelas já escolhidas) — empate vai
    pro maior score, já que `tables` chega ordenada por score desc. Para
    quando ninguém mais acrescenta nenhuma keyword nova, mesmo com vagas
    sobrando em `n`: uma 6ª tabela que só repete o que as outras 5 já
    cobrem não ajuda o passo 3 a ler nada de novo.
    """
    selected: list[Table] = []
    covered: set[str] = set()
    remaining = list(tables)
    while remaining and len(selected) < n:
        best, best_new = None, 0
        for t in remaining:
            new_count = len(set(t.keywords) - covered)
            if new_count > best_new:
                best, best_new = t, new_count
        if best is None:
            break
        selected.append(best)
        covered |= set(best.keywords)
        remaining.remove(best)
    return selected


def _score_table(
    bbox,
    text_boxes: list[TextBox],
    keywords: list[str] | None = None,
    *,
    include_generic_bonuses: bool = True,
) -> Table:
    """Soma o score de keywords de todos os textos dentro do bbox.

    Também mede a altura do texto de CORPO desta tabela (percentil 25 das
    alturas internas) — usada para calcular o DPI individual no render,
    já que cada tabela tem sua própria escala de fonte.

    `keywords=None` usa a lista padrão de `_text_score` (_KEYWORDS_HIGH_VALUE)."""
    from api import _text_score
    from dxf_render import clean_mtext_preview

    kwargs = {} if keywords is None else {"keywords": keywords}
    x0, y0, x1, y1 = bbox
    total = 0.0
    kw = 0
    n = 0
    kw_texts: list[str] = []
    heights: list[float] = []
    for tb in text_boxes:
        if x0 <= tb.cx <= x1 and y0 <= tb.cy <= y1:
            s = _text_score(
                tb.text, include_generic_bonuses=include_generic_bonuses, **kwargs
            )
            total += s
            n += 1
            if tb.height > 0:
                heights.append(tb.height)
            if s > 1.0:
                kw += 1
                kw_texts.append(clean_mtext_preview(tb.text, max_len=40))

    # Altura de corpo = percentil 25 (ignora títulos grandes da tabela)
    if heights:
        heights.sort()
        body_h = heights[int(len(heights) * 0.25)]
    else:
        body_h = 0.0

    return Table(
        bbox=bbox,
        score=total,
        keyword_count=kw,
        text_count=n,
        keywords=kw_texts,
        text_height=body_h,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PASSO 1 — Avaliar a qualidade do DXF
# ─────────────────────────────────────────────────────────────────────────────


def _avaliar_qualidade(doc, msp) -> tuple[str, str]:
    """Classifica o DXF em alta/media/baixa. Retorna (qualidade, motivo)."""
    from api import _detect_quality, _text_height_spread
    from dxf_render import analyze_dxf, collect_text_positions

    info = analyze_dxf(doc)
    positions = collect_text_positions(msp, layer=None)
    spread = _text_height_spread(msp)
    return _detect_quality(info, positions, height_spread=spread)


# ─────────────────────────────────────────────────────────────────────────────
# PASSO 2 — Medir a escala do texto (base para a resolução do grid)
# ─────────────────────────────────────────────────────────────────────────────


def _medir_escala_texto(msp) -> float:
    """Altura típica do texto de corpo (unidades DXF). Nunca zero."""
    from api import _text_height_dxf

    return _text_height_dxf(msp) or 1.0


# ─────────────────────────────────────────────────────────────────────────────
# PASSO 4 — Registrar o resultado no log do uvicorn
# ─────────────────────────────────────────────────────────────────────────────


def _log_resultado(qualidade, motivo, text_height, cell, gap_cells, tables):
    """Loga qualidade, parâmetros do grid e as tabelas detectadas."""
    _log.warning("─" * 70)
    _log.warning("[table_pipeline] QUALIDADE=%s", qualidade.upper())
    _log.warning("[table_pipeline] motivo: %s", motivo)
    _log.warning(
        "[table_pipeline] text_height=%.2f cell=%.2f gap_cells=%d tabelas=%d",
        text_height,
        cell,
        gap_cells,
        len(tables),
    )
    for i, t in enumerate(tables, 1):
        _log.warning(
            "  tabela %d: score=%.0f kw=%d textos=%d txt_h=%.2f "
            "bbox=(%.0f,%.0f)-(%.0f,%.0f) %.0fx%.0f  kws=%s",
            i,
            t.score,
            t.keyword_count,
            t.text_count,
            t.text_height,
            t.bbox[0],
            t.bbox[1],
            t.bbox[2],
            t.bbox[3],
            t.width,
            t.height,
            ", ".join(t.keywords[:4]),
        )
    _log.warning("─" * 70)


# ─────────────────────────────────────────────────────────────────────────────
# Orquestração — PASSOS 1 → 4
# ─────────────────────────────────────────────────────────────────────────────


def run_pipeline(
    doc,
    *,
    n: int = 5,
    cell_factor: float = 1.0,
    gap_factor: float = 2.5,
    roi_margin_factor: float = 60.0,
    group_factor: float = 25.0,
) -> PipelineResult:
    """Detecta as tabelas PSCIP de um documento DXF (passos 1 a 4).

    A renderização em alta resolução (passo 5) fica em ``render_tables``,
    chamada depois que o consumidor decide extrair.

    Raises:
        LowQualityDXFError: DXF de qualidade baixa — extração não compensa.

    Returns:
        PipelineResult com as tabelas detectadas (lista pode vir vazia se
        nenhuma região tiver keywords PSCIP).
    """
    msp = doc.modelspace()

    # ═══ PASSO 1 — Avaliar a qualidade do DXF ═══
    qualidade, motivo = _avaliar_qualidade(doc, msp)
    if qualidade == "baixa":
        _log.warning("[table_pipeline] qualidade BAIXA → abortando: %s", motivo)
        raise LowQualityDXFError(qualidade, motivo)

    # ═══ PASSO 2 — Medir a escala do texto ═══
    text_height = _medir_escala_texto(msp)

    # ═══ PASSO 3 — Detectar tabelas (grade conectada + score por keywords) ═══
    tables, cell, gap_cells = detect_tables(
        msp,
        text_height,
        cell_factor=cell_factor,
        gap_factor=gap_factor,
        roi_margin_factor=roi_margin_factor,
        group_factor=group_factor,
    )
    tables = _select_diverse(tables, n)

    # ═══ PASSO 4 — Registrar no log ═══
    _log_resultado(qualidade, motivo, text_height, cell, gap_cells, tables)

    return PipelineResult(
        qualidade=qualidade,
        motivo=motivo,
        tables=tables,
        text_height=text_height,
        cell=cell,
        gap_cells=gap_cells,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PASSO 5 — Render das tabelas em alta resolução (compartilhado endpoint/script)
# ─────────────────────────────────────────────────────────────────────────────

# Altura-alvo do texto no PNG final (px) para um LLM ler com folga.
TARGET_TEXT_PX = 22
MIN_DPI = 40
MAX_OUTPUT_PX = 6000


def legible_dpi(text_height: float) -> int:
    """DPI que faz o texto ter ~TARGET_TEXT_PX no output.

    px = unidades/72 × dpi  →  para o texto: text_px = text_height/72 × dpi.
    """
    if text_height <= 0:
        return 150
    return max(int(TARGET_TEXT_PX * 72 / text_height), MIN_DPI)


def _regiao_capada(doc, bbox, raio, max_ent, cache):
    """Expande `bbox` em `raio` unidades por lado, encolhendo até caber no teto.

    Cresce simétrico e para quando o nº de entidades da região passa de
    `max_ent`, que é o que infla o custo de IA e o render numa prancha densa.
    O raio é ABSOLUTO (em altura de texto), e é isso que limita o tamanho: um
    componente que é só o título do detalhe vira uma região do mesmo tamanho
    que um componente-tabela. Não há teto de área relativo ao bbox: esse teto
    estrangulava justamente o caso que a região existe para resolver (o título
    é minúsculo, 3,5x a área dele não alcança o desenho ao lado), e o engolir a
    prancha que ele evitava era efeito de padding PERCENTUAL, não de raio fixo.
    Devolve o próprio bbox se nada couber.
    """
    from dxf_render import filter_entities_by_bbox

    def n_ent(b):
        return len(list(filter_entities_by_bbox(doc.modelspace(), b, cache=cache)))

    alvo = (bbox[0] - raio, bbox[1] - raio, bbox[2] + raio, bbox[3] + raio)
    cabe = lambda b: n_ent(b) <= max_ent
    if cabe(alvo):
        return alvo
    lo, hi = 0.0, 1.0
    for _ in range(7):
        f = (lo + hi) / 2
        cand = tuple(bbox[i] + f * (alvo[i] - bbox[i]) for i in range(4))
        if cabe(cand):
            lo = f
        else:
            hi = f
    return tuple(bbox[i] + lo * (alvo[i] - bbox[i]) for i in range(4))


def render_tables(doc, result: PipelineResult) -> list[dict]:
    """Renderiza cada tabela do resultado em alta resolução.

    Lógica única usada tanto pelo endpoint /tables/extract quanto pelo script
    batch — garante que ambos produzem PNGs idênticos.

    Returns:
        Lista de dicts: {name, png (bytes), score, keyword_count, text_count,
        text_height, dpi, bbox, keywords}.
    """
    import os
    import tempfile
    from ezdxf import bbox
    from dxf_render import build_config, suggest_lineweight, render_overview_with_rects
    from ezdxf.addons.drawing.config import ColorPolicy

    if not result.tables:
        return []

    # Cache de bounding boxes COMPARTILHADO entre as N tabelas. Sem ele, cada
    # render refiltra as entidades recalculando a bbox do documento inteiro do
    # zero — em pranchas grandes (centenas de milhares de entidades) isso domina
    # o tempo e estoura o timeout do cliente. Reutilizar o cache paga o custo do
    # índice uma única vez. Medido em prancha de 187k entidades: 117s → 46s.
    bbox_cache = bbox.Cache()

    # Render de tabelas para LLM/OCR: TUDO PRETO sobre fundo branco.
    # Cor não importa para extração de dados — legibilidade sim. Forçar preto
    # elimina o caso em que texto cor-7 (BYLAYER branco) fica invisível porque
    # o auto-detect global decidiu não inverter (acerta a planta, erra a tabela).
    # Fundos preenchidos (HATCH/WIPEOUT) já são excluídos no render, então não
    # há risco de "preto sobre preto".
    color_policy = ColorPolicy.BLACK
    pad = result.cell * 1.5  # folga p/ a linha externa aparecer inteira

    out: list[dict] = []
    # Recorte de REGIÃO (opt-in por VISAO_REGIAO_RAIO_TH > 0). O recorte padrão é o
    # bbox justo do componente (a tabela de texto), e o detalhe que a norma pede,
    # um desenho colado ao lado, cai fora. Expandir o recorte para uma região em
    # volta do componente, com raio absoluto em altura de texto e teto de
    # entidades, traz o desenho para dentro. Medido sobre 20 regras/8 projetos com
    # a IA de produção, na régua rotulada à mão: o raio 35 ainda cortava o
    # conteúdo deslocado do título (memorial à direita, desenho acima); o raio 45
    # recuperou o detalhe inteiro (desenho + cota de altura) e, mais que isso,
    # deu veredito ESTÁVEL onde o crop cortado fazia a IA oscilar entre runs.
    # Recomendado ao ligar: 45. O anchoring no termo específico do detalhe é do
    # chamador via keywords (trem-api, ItemIT03.ancoras); aqui só a geometria.
    raio_th = float(os.getenv("VISAO_REGIAO_RAIO_TH", "0") or 0)
    regiao_max_ent = int(os.getenv("VISAO_REGIAO_MAX_ENTIDADES", "8000") or 8000)

    for i, t in enumerate(result.tables, 1):
        x0, y0, x1, y1 = t.bbox
        region = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        local_h_regiao = t.text_height if t.text_height > 0 else result.text_height
        if raio_th > 0 and local_h_regiao > 0:
            region = _regiao_capada(
                doc, region, raio_th * local_h_regiao, regiao_max_ent, bbox_cache,
            )
        side_ref = max(t.width, t.height, result.cell)
        config = build_config(
            color_policy=color_policy, min_lineweight=suggest_lineweight(side_ref, 200)
        )

        # DPI dinâmico por tabela (altura do texto desta tabela)
        local_h = t.text_height if t.text_height > 0 else result.text_height
        dpi = legible_dpi(local_h)

        png_path = tempfile.mktemp(suffix=".png")
        try:
            ok = render_overview_with_rects(
                doc,
                rects=[],
                output_path=png_path,
                region=region,
                dpi=dpi,
                max_px=MAX_OUTPUT_PX,
                config=config,
                bbox_cache=bbox_cache,
                verbose=False,
            )
            if not ok:
                continue
            with open(png_path, "rb") as f:
                png = f.read()
        finally:
            try:
                if os.path.exists(png_path):
                    os.unlink(png_path)
            except OSError:
                pass

        name = f"tabela_{i:02d}_score{t.score:.0f}_kw{t.keyword_count}.png"
        out.append(
            {
                "name": name,
                "png": png,
                "score": t.score,
                "keyword_count": t.keyword_count,
                "text_count": t.text_count,
                "text_height": local_h,
                "dpi": dpi,
                "bbox": t.bbox,
                "keywords": t.keywords,
            }
        )
    return out
