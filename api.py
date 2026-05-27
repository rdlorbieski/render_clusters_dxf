from __future__ import annotations

# CRÍTICO: forçar backend não-GUI ANTES de qualquer import que carregue pyplot
# (ezdxf.addons.drawing.matplotlib puxa pyplot via dxf_render).
# Sem isso, em servidor o matplotlib tenta abrir janela Tk e estoura memória.
import matplotlib
matplotlib.use("Agg")

import csv
import io
import os
import re
import tempfile
import zipfile
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from ezdxf import recover

from dxf_render import (
    analyze_dxf,
    auto_detect_color_policy,
    build_config,
    Cluster,
    find_dominant_text_layer,
    find_top_clusters,
    collect_text_positions,
    render_region,
    suggest_lineweight,
    suggest_region_size,
)

app = FastAPI(
    title="DXF Render API",
    description="Renderiza regiões de DXFs e detecta clusters de texto.",
    version="1.0.0",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_dxf_to_tmp(file: UploadFile) -> str:
    """Salva o DXF enviado em arquivo temporário. Retorna o caminho."""
    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    try:
        tmp.write(file.file.read())
        tmp.flush()
        return tmp.name
    finally:
        tmp.close()


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de side e render
# ─────────────────────────────────────────────────────────────────────────────


_PURE_NUMBER_RE = re.compile(r"^[-+]?\d+([.,]\d+)?$")

# Perfis de "qualidade" do DXF — ajustam side, margem e DPI alvo.
# - "alta":  DXF bem-organizado, escala normal. Janelas justas, render apertado.
# - "media": default. Compromisso entre legibilidade e contexto.
# - "baixa": DXF problemático (paper-space, layers caóticas). Mais margem,
#            render maior pra conseguir ler mesmo com escolha imprecisa de centro.
# Cada tupla é (side_multiplier, render_margin, target_px).
_QUALITY_PARAMS: dict[str, tuple[float, float, int]] = {
    "alta":  (0.8, 0.30, 3500),
    "media": (1.0, 0.40, 4500),
    "baixa": (2.0, 0.60, 6000),
}


def _get_quality(q: str) -> tuple[float, float, int]:
    """Retorna (side_mult, margin, target_px) — default 'media' se inválido."""
    return _QUALITY_PARAMS.get((q or "media").lower(), _QUALITY_PARAMS["media"])


def _detect_quality(info, positions: list | None = None) -> tuple[str, str]:
    """
    Classifica automaticamente a qualidade do DXF como 'alta', 'media' ou 'baixa'
    baseado em sinais estruturais. Retorna (qualidade, motivo).

    Sinais:
    - paper-space (min < 500): baixa — densidade colapsa, escala minúscula
    - aspect ratio extremo (>30): baixa — clusters mal-distribuídos no eixo longo
    - modelspace amplo + aspect razoável + boa contagem: alta
    - resto: media (a maioria dos casos)
    """
    if not info.extents:
        return "media", "extents indisponíveis"

    dx = info.extents[2] - info.extents[0]
    dy = info.extents[3] - info.extents[1]
    shorter = min(dx, dy)
    longer = max(dx, dy)
    aspect = longer / max(shorter, 0.001)
    n_texts = len(positions) if positions else info.n_texts

    if shorter < 500:
        return "baixa", f"paper-space detectado (dimensão menor = {shorter:.0f} unidades)"
    if aspect > 30:
        return "baixa", f"aspect ratio extremo ({aspect:.0f}:1)"
    if shorter >= 5000 and aspect <= 5 and n_texts >= 200:
        return "alta", f"modelspace bem-formado (aspect {aspect:.1f}, {n_texts} textos)"
    return "media", f"aspect {aspect:.1f}, dim menor {shorter:.0f}, {n_texts} textos"

# Palavras/expressões que indicam um cluster valioso (carimbo de RT,
# quadro informativo, listas de medidas preventivas, classificação de
# ocupação). Comparação é case-insensitive contra texto já limpo dos
# códigos MTEXT.
_KEYWORDS_HIGH_VALUE = [
    "RESPONSÁVEL TÉCNICO", "RESP. TÉCNICO", "RESP TECNICO",
    "RESPONSAVEL TECNICO", "CREA", "ÁREA", "1:100",
    "SAÍDAS DE EMERGÊNCIA", "SAIDAS DE EMERGENCIA",
    "EXTINTOR",
    "ILUMINAÇÃO DE EMERGÊNCIA", "ILUMINACAO DE EMERGENCIA",
    "SINALIZAÇÃO DE EMERGÊNCIA", "SINALIZACAO DE EMERGENCIA",
    "ACESSO DE VIATURA",
    "CONTROLE DE MATERIAIS",
    "SUBESTAÇÃO DE ENERGIA", "SUBESTACAO DE ENERGIA",
]
# RT como token isolado (não confunde com palavras tipo "PARTE")
_RT_RE = re.compile(r"\bRT\b")
# Classificação de ocupação: A-1, M-3, F-8, etc.
_OCC_CLASS_RE = re.compile(r"\b[A-Z]-\d\b")


def _text_score(text: str) -> float:
    """
    Pontua um texto pela presença de keywords valiosas.
    Base = 1.0 (qualquer texto não-vazio).
    +10 por cada keyword. +5 por padrão LETRA-DÍGITO. +10 por 'RT' isolado.
    """
    from dxf_render import clean_mtext_preview
    if not text:
        return 0.0
    cleaned = clean_mtext_preview(text, max_len=200).upper()
    if not cleaned:
        return 0.0
    score = 1.0
    for kw in _KEYWORDS_HIGH_VALUE:
        if kw in cleaned:
            score += 10.0
    if _RT_RE.search(cleaned):
        score += 10.0
    if _OCC_CLASS_RE.search(cleaned):
        score += 5.0
    return score


def _find_top_clusters_scored(positions, n: int, side: float):
    """
    Greedy NMS por SOMA DE SCORES (não contagem).
    Retorna lista de tuplas (Cluster, total_score, keyword_count).
    """
    if not positions:
        return []
    scored = [(p, _text_score(p.text)) for p in positions]
    half = side / 2
    remaining = list(scored)
    out = []

    for _ in range(n):
        if not remaining:
            break

        best_score = 0.0
        best_window: list = []
        for tp, _s in remaining:
            window = [(p, s) for p, s in remaining
                      if abs(p.x - tp.x) <= half and abs(p.y - tp.y) <= half]
            ws = sum(s for _, s in window)
            if ws > best_score:
                best_score = ws
                best_window = window

        if not best_window:
            break

        wx = sum(p.x for p, _ in best_window) / len(best_window)
        wy = sum(p.y for p, _ in best_window) / len(best_window)
        final = [(p, s) for p, s in remaining
                 if abs(p.x - wx) <= half and abs(p.y - wy) <= half]
        members = [p for p, _ in final]
        total = sum(s for _, s in final)
        kw = sum(1 for _, s in final if s > 1.0)
        out.append((Cluster(cx=wx, cy=wy, side=side, members=members), total, kw))

        used = {(p.x, p.y) for p, _ in final}
        remaining = [(p, s) for p, s in remaining if (p.x, p.y) not in used]

    return out


def _filter_meaningful(positions: list) -> list:
    """
    Remove TextPos cujo conteúdo é puramente numérico (item nº, cotas,
    sequências 1, 2, 3...). Esses textos inflam o text_count dos clusters
    sem contribuir com informação semântica.
    Se o filtro deixar a lista vazia ou pequena demais, retorna a original
    (fallback para DXFs onde quase todos os textos são números).
    """
    from dxf_render import clean_mtext_preview
    out = []
    for tp in positions:
        cleaned = clean_mtext_preview(tp.text, max_len=100).strip()
        if cleaned and not _PURE_NUMBER_RE.match(cleaned):
            out.append(tp)
    return out if len(out) >= 3 else positions


def _density_side(positions: list, k: int = 15, factor: float = 2.0) -> float:
    """
    Side derivado da densidade real dos textos — totalmente independente
    de unidades, escala ou aspect ratio do DXF.

    Para cada ponto, calcula a distância ao k-ésimo vizinho mais próximo.
    A mediana dessas distâncias × factor define o lado da janela:
    pequeno o suficiente pra distinguir concentrações locais, grande
    o suficiente pra agrupar textos relacionados.

    Args:
        positions: lista de TextPos
        k: nº de vizinhos a considerar (15 = janela com ~k textos)
        factor: multiplicador da mediana (≥1.5 dá margem confortável)
    """
    n = len(positions)
    if n < 3:
        return 100.0

    import random
    random.seed(42)
    sample = random.sample(positions, min(200, n))
    k_eff = min(k, n - 1)

    knn = []
    for p in sample:
        dists_sq = [(p.x - q.x) ** 2 + (p.y - q.y) ** 2
                    for q in positions if q is not p]
        if len(dists_sq) >= k_eff:
            dists_sq.sort()
            knn.append(dists_sq[k_eff - 1] ** 0.5)

    if not knn:
        return 100.0

    knn.sort()
    median = knn[len(knn) // 2]
    return max(median * factor, 1.0)


def _smart_side(info, positions: list | None = None) -> float:
    """
    Combina densidade real dos textos + sanity bounds baseados em extents.

    - Para a maioria dos DXFs (modelspace em metros/km), a densidade é
      o sinal certo: adapta a qualquer escala.
    - Para DXFs em paper-space (dimensão menor < 500 unidades, ex.: mm),
      a densidade colapsa porque textos ficam super juntos. Aí usamos a
      dimensão menor como floor — garante janela proporcional ao desenho.
    """
    density = None
    if positions and len(positions) >= 3:
        density = _density_side(positions)

    if info.extents:
        dx = info.extents[2] - info.extents[0]
        dy = info.extents[3] - info.extents[1]
        shorter, longer = min(dx, dy), max(dx, dy)

        # Paper-space / DXF em escala minúscula: density colapsa, usa altura.
        if shorter < 500:
            return max(density or 0, shorter * 1.5)

        # Modelspace plano (aspect > 10:1) sem density disponível:
        # usa dimensão menor como floor.
        if density is None and longer / max(shorter, 0.001) > 10:
            return shorter

    if density is not None:
        return density
    return suggest_region_size(info) or 1000.0


def _render_at(doc, cx: float, cy: float, rside: float, config,
               target_px: int = 4500) -> bytes | None:
    """
    Renderiza em (cx, cy) EXATOS — usado por /render-region.
    Não tenta recentralizar baseado em conteúdo; o ponto é a verdade.
    DPI adaptativo para manter ~target_px de lado.
    """
    dpi = max(150, min(400, int(target_px * 72 / rside)))
    png_path = tempfile.mktemp(suffix=".png")
    try:
        ok = render_region(doc, cx, cy, rside, png_path,
                           dpi=dpi, config=config, verbose=False)
        if not ok:
            return None
        with open(png_path, "rb") as f:
            return f.read()
    finally:
        _cleanup(png_path)


def _render_cluster(doc, cluster: Cluster, side: float, config,
                    margin: float = 0.40, target_px: int = 4500) -> bytes | None:
    """
    Renderiza um cluster usando o bbox REAL dos seus membros + margem.
    A janela emerge naturalmente do conteúdo — funciona em qualquer escala.
    `side` serve só como piso (min_side) pra clusters com poucos membros.

    `margin` e `target_px` vêm do perfil de qualidade do DXF.
    """
    rcx, rcy, rside, *_ = cluster.render_bounds(margin=margin, min_side=side)

    # Insert points ficam na borda esquerda do texto — desloca centro para direita
    if cluster.members:
        x_min = min(p.x for p in cluster.members)
        rcx = x_min + rside * 0.35  # 15% padding à esquerda, 85% à direita

    # DPI adaptativo
    dpi = max(150, min(400, int(target_px * 72 / rside)))

    png_path = tempfile.mktemp(suffix=".png")
    try:
        ok = render_region(doc, rcx, rcy, rside, png_path,
                           dpi=dpi, config=config, verbose=False)
        if not ok:
            return None
        with open(png_path, "rb") as f:
            return f.read()
    finally:
        _cleanup(png_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 1 — Renderizar região
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/render-region",
    summary="Renderiza região ao redor de (x, y) e retorna o PNG como download",
)
async def render_region_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF")],
    x: Annotated[float, Query(description="Coordenada X do centro da região")],
    y: Annotated[float, Query(description="Coordenada Y do centro da região")],
):
    dxf_path = _load_dxf_to_tmp(file)
    png_path: str | None = None

    try:
        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        dominant_layer = find_dominant_text_layer(msp)

        info = analyze_dxf(doc)
        color_policy = auto_detect_color_policy(info)
        all_texts = collect_text_positions(msp, layer=dominant_layer)
        qualidade, _motivo = _detect_quality(info, all_texts)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = _smart_side(info, all_texts) * side_mult
        min_lw = suggest_lineweight(side, 200)
        config = build_config(color_policy=color_policy, min_lineweight=min_lw)

        # USA O PONTO DO USUÁRIO COMO CENTRO EXATO.
        # rside parte de side*(1+2*margin), mas expande se houver textos
        # próximos cujo spread exija mais espaço.
        rside = side * (1 + 2 * margin)
        meaningful_nearby = _filter_meaningful([
            p for p in all_texts
            if abs(p.x - x) <= side and abs(p.y - y) <= side
        ])
        if meaningful_nearby:
            max_off = max(
                max(abs(p.x - x) for p in meaningful_nearby),
                max(abs(p.y - y) for p in meaningful_nearby),
            )
            # cobre os textos vizinhos + extra proporcional à margem
            rside = max(rside, max_off * (2 + 2 * margin))

        png_bytes = _render_at(doc, x, y, rside, config, target_px=target_px)

        if not png_bytes:
            raise HTTPException(
                status_code=404,
                detail="Região vazia — nenhuma entidade encontrada no ponto informado.",
            )

        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "Content-Disposition": 'attachment; filename="region.png"',
                "X-Qualidade-DWG": qualidade,
            },
        )

    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 2 — Top clusters de texto
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/top-clusters",
    summary="Retorna os N clusters com mais textos na layer dominante",
    response_description="Lista de centros de cluster ordenados por densidade",
)
async def top_clusters_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF")],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a retornar"),
    ] = 5,
):
    dxf_path = _load_dxf_to_tmp(file)

    try:
        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        dominant_layer = find_dominant_text_layer(msp)
        if dominant_layer is None:
            raise HTTPException(
                status_code=422,
                detail="Nenhuma entidade TEXT/MTEXT encontrada no arquivo.",
            )

        info = analyze_dxf(doc)
        # Coleta de TODAS as layers: keywords PSCIP frequentemente estão
        # em layers não-dominantes (carimbo em layer separada, quadros em
        # layers próprias). O scoring vai priorizar os clusters certos.
        positions = collect_text_positions(msp, layer=None)
        if not positions:
            raise HTTPException(
                status_code=422,
                detail="Nenhuma entidade TEXT/MTEXT encontrada no arquivo.",
            )

        # Ignora textos puramente numéricos (item nº, cotas) que inflariam o text_count
        positions = _filter_meaningful(positions)

        qualidade, motivo = _detect_quality(info, positions)
        side_mult, _, _ = _get_quality(qualidade)
        side = _smart_side(info, positions) * side_mult
        scored = _find_top_clusters_scored(positions, n=n, side=side)

        return JSONResponse({
            "qualidade_dwg": qualidade,
            "qualidade_motivo": motivo,
            "layer_used": "<all>",
            "dominant_layer": dominant_layer,
            "total_texts": len(positions),
            "clusters": [
                {
                    "x": round(c.cx, 4),
                    "y": round(c.cy, 4),
                    "text_count": c.count,
                    "keyword_count": kw,
                    "score": round(total, 2),
                }
                for c, total, kw in scored
            ],
        })

    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 3 — Top clusters a partir de valores de um CSV
# ─────────────────────────────────────────────────────────────────────────────


def _csv_tokens(csv_bytes: bytes, min_len: int = 4) -> set[str]:
    """
    Extrai todos os tokens únicos do CSV que valem a pena buscar no DXF.
    Cada célula é quebrada em tokens (palavras/números), descartando os
    muito curtos ou puramente numéricos de 1-2 dígitos.
    """
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    tokens: set[str] = set()
    for row in reader:
        for value in row.values():
            if not value:
                continue
            # inclui o valor inteiro (normalizado) e cada token individual
            normalized = value.strip()
            if len(normalized) >= min_len:
                tokens.add(normalized.upper())
            for tok in re.split(r"[\s,;/\\]+", normalized):
                tok = tok.strip()
                if len(tok) >= min_len:
                    tokens.add(tok.upper())
    return tokens


def _collect_csv_matches(msp, tokens: set[str]) -> list:
    """
    Retorna TextPos de entidades cujo texto (limpo, uppercase) contém
    pelo menos um token do CSV.
    """
    from dxf_render import TextPos, clean_mtext_preview
    matches = []
    for e in msp:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            p = e.dxf.insert
            raw = (e.dxf.text if e.dxftype() == "TEXT"
                   else getattr(e.dxf, "text", ""))
            if not raw or not raw.strip():
                continue
            cleaned = clean_mtext_preview(raw, max_len=500).upper()
            if any(tok in cleaned for tok in tokens):
                matches.append(TextPos(p.x, p.y, raw))
        except Exception:
            pass
    return matches


@app.post(
    "/top-clusters-csv",
    summary="Encontra as regiões do DXF onde os dados do CSV aparecem",
    response_description="Lista de centros de cluster ordenados por densidade de matches",
)
async def top_clusters_csv_endpoint(
    dxf: Annotated[UploadFile, File(description="Arquivo DXF")],
    csv_file: Annotated[UploadFile, File(description="Arquivo CSV com os dados")],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a retornar"),
    ] = 5,
):
    dxf_path = _load_dxf_to_tmp(dxf)

    try:
        csv_bytes = await csv_file.read()
        tokens = _csv_tokens(csv_bytes)
        if not tokens:
            raise HTTPException(status_code=422, detail="CSV não contém valores utilizáveis.")

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            raise HTTPException(
                status_code=404,
                detail="Nenhum texto do DXF corresponde aos valores do CSV.",
            )

        info = analyze_dxf(doc)
        qualidade, motivo = _detect_quality(info, matches)
        side_mult, _, _ = _get_quality(qualidade)
        side = _smart_side(info, matches) * side_mult

        clusters = find_top_clusters(matches, n_clusters=n, side=side)

        return JSONResponse({
            "qualidade_dwg": qualidade,
            "qualidade_motivo": motivo,
            "tokens_searched": len(tokens),
            "texts_matched": len(matches),
            "clusters": [
                {"x": round(c.cx, 4), "y": round(c.cy, 4), "match_count": c.count}
                for c in clusters
            ],
        })

    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 4 — Render de todos os clusters CSV em um ZIP
# ─────────────────────────────────────────────────────────────────────────────


# _render_center substituído por _render_cluster (definido acima)


@app.post(
    "/render-clusters-csv",
    summary="Renderiza todas as regiões dos clusters CSV e retorna um ZIP",
)
async def render_clusters_csv_endpoint(
    dxf: Annotated[UploadFile, File(description="Arquivo DXF")],
    csv_file: Annotated[UploadFile, File(description="Arquivo CSV com os dados")],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a renderizar"),
    ] = 5,
):
    dxf_path = _load_dxf_to_tmp(dxf)

    try:
        csv_bytes = await csv_file.read()
        tokens = _csv_tokens(csv_bytes)
        if not tokens:
            raise HTTPException(status_code=422, detail="CSV não contém valores utilizáveis.")

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        dominant_layer = find_dominant_text_layer(msp)

        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            raise HTTPException(status_code=404,
                                detail="Nenhum texto do DXF corresponde aos valores do CSV.")

        info = analyze_dxf(doc)
        qualidade, _ = _detect_quality(info, matches)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = _smart_side(info, matches) * side_mult
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side, 200))

        clusters = find_top_clusters(matches, n_clusters=n, side=side)

        zip_buf = io.BytesIO()
        rendered = 0
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, c in enumerate(clusters, 1):
                png_bytes = _render_cluster(doc, c, side, config,
                                            margin=margin, target_px=target_px)
                if png_bytes:
                    zf.writestr(f"cluster_{i:02d}_matches{c.count}.png", png_bytes)
                    rendered += 1

        if rendered == 0:
            raise HTTPException(status_code=404,
                                detail="Nenhuma região continha entidades renderizáveis.")

        zip_buf.seek(0)
        return Response(
            content=zip_buf.read(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="clusters.zip"',
                "X-Qualidade-DWG": qualidade,
            },
        )

    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint auxiliar — Diagnóstico do DXF
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/diagnose", summary="Diagnóstico de layers, textos e side sugerido")
async def diagnose_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF")],
):
    dxf_path = _load_dxf_to_tmp(file)
    try:
        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()
        info = analyze_dxf(doc)

        # Contagem de textos por layer
        layer_counts: dict[str, int] = {}
        for e in msp:
            if e.dxftype() in ("TEXT", "MTEXT"):
                layer = e.dxf.get("layer", "0")
                layer_counts[layer] = layer_counts.get(layer, 0) + 1
        layers_sorted = sorted(layer_counts.items(), key=lambda x: -x[1])

        dominant = layers_sorted[0][0] if layers_sorted else None
        all_texts = collect_text_positions(msp, layer=dominant)

        # Estatísticas de dispersão dos textos na layer dominante
        text_stats = {}
        if all_texts:
            xs = [p.x for p in all_texts]
            ys = [p.y for p in all_texts]
            text_stats = {
                "x_min": round(min(xs), 2), "x_max": round(max(xs), 2),
                "y_min": round(min(ys), 2), "y_max": round(max(ys), 2),
                "x_spread": round(max(xs) - min(xs), 2),
                "y_spread": round(max(ys) - min(ys), 2),
            }

        side = _smart_side(info)

        return JSONResponse({
            "extents": info.extents and {
                "x_min": round(info.extents[0], 2), "y_min": round(info.extents[1], 2),
                "x_max": round(info.extents[2], 2), "y_max": round(info.extents[3], 2),
                "width": round(info.extents[2] - info.extents[0], 2),
                "height": round(info.extents[3] - info.extents[1], 2),
            },
            "total_entities": info.n_entities,
            "total_texts": info.n_texts,
            "suggested_side": round(side, 2),
            "top_layers_by_text": [
                {"layer": l, "text_count": c} for l, c in layers_sorted[:10]
            ],
            "dominant_layer": dominant,
            "dominant_layer_text_spread": text_stats,
        })
    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 5 — Render de clusters a partir de strings livres
# ─────────────────────────────────────────────────────────────────────────────


def _tokens_from_text(raw: str, min_len: int = 4) -> set[str]:
    """Tokeniza um bloco de texto livre (uma string por linha)."""
    tokens: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) >= min_len:
            tokens.add(line.upper())
        for tok in re.split(r"[\s,;/\\]+", line):
            tok = tok.strip()
            if len(tok) >= min_len:
                tokens.add(tok.upper())
    return tokens


@app.post(
    "/render-clusters-strings",
    summary="Busca strings no DXF, renderiza os clusters e retorna um ZIP",
)
async def render_clusters_strings_endpoint(
    dxf: Annotated[UploadFile, File(description="Arquivo DXF")],
    strings: Annotated[
        str,
        Form(description="Valores a buscar no DXF — um por linha"),
    ],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a renderizar"),
    ] = 5,
):
    dxf_path = _load_dxf_to_tmp(dxf)

    try:
        tokens = _tokens_from_text(strings)
        if not tokens:
            raise HTTPException(status_code=422, detail="Nenhuma string válida fornecida.")

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        dominant_layer = find_dominant_text_layer(msp)

        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            raise HTTPException(status_code=404,
                                detail="Nenhum texto do DXF corresponde às strings fornecidas.")

        info = analyze_dxf(doc)
        qualidade, _ = _detect_quality(info, matches)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = _smart_side(info, matches) * side_mult
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side, 200))

        clusters = find_top_clusters(matches, n_clusters=n, side=side)

        zip_buf = io.BytesIO()
        rendered = 0
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, c in enumerate(clusters, 1):
                png_bytes = _render_cluster(doc, c, side, config,
                                            margin=margin, target_px=target_px)
                if png_bytes:
                    zf.writestr(f"cluster_{i:02d}_matches{c.count}.png", png_bytes)
                    rendered += 1

        if rendered == 0:
            raise HTTPException(status_code=404,
                                detail="Nenhuma região continha entidades renderizáveis.")

        zip_buf.seek(0)
        return Response(
            content=zip_buf.read(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="clusters.zip"',
                "X-Qualidade-DWG": qualidade,
            },
        )

    finally:
        _cleanup(dxf_path)
