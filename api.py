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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated

import numpy as np
from PIL import Image

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from ezdxf import recover
from ezdxf import bbox as ezdxf_bbox

from converter import convert_dwg_to_dxf

from dxf_render import (
    analyze_dxf,
    auto_detect_color_policy,
    build_config,
    Cluster,
    find_dominant_text_layer,
    collect_text_positions,
    render_region,
    render_overview_with_rects,
    suggest_lineweight,
    suggest_region_size,
)

app = FastAPI(
    title="DXF Render API",
    description="Renderiza regiões de DXFs/DWGs e detecta clusters de texto.",
    version="1.1.0",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_dxf_to_tmp(file: UploadFile) -> str:
    """Salva o arquivo enviado em temporário e retorna o caminho de um DXF.

    Aceita .dxf e .dwg. Arquivos DWG são convertidos para DXF via ODA File
    Converter antes de retornar — o chamador recebe sempre um .dxf.
    """
    filename = (file.filename or "").lower()
    is_dwg = filename.endswith(".dwg")
    if not is_dwg and not filename.endswith(".dxf"):
        raise HTTPException(
            status_code=422,
            detail="O arquivo deve ter extensão .dxf ou .dwg.",
        )

    suffix = ".dwg" if is_dwg else ".dxf"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(file.file.read())
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    if not is_dwg:
        return tmp_path

    # Converte DWG → DXF e limpa o arquivo DWG temporário.
    try:
        dxf_path = convert_dwg_to_dxf(tmp_path)
        return str(dxf_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RuntimeError, Exception) as exc:
        raise HTTPException(
            status_code=422, detail=f"Falha na conversão DWG → DXF: {exc}"
        ) from exc
    finally:
        _cleanup(tmp_path)


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


_PURE_NUMBER_RE    = re.compile(r"^[-+]?\d+([.,]\d+)?$")
# Detecta texto com letra-espaçada: ≥60% dos tokens são 1 caractere.
# Ex: "S I N A L I Z A Ç Ã O" → "SINALIZAÇÃO"
_SPACED_TEXT_RATIO = 0.60


def _normalize_spaced(text: str) -> str:
    """Colapsa texto letra-espaçada preservando limites de palavra.

    'S I N A L I Z A C A O  D E  E M E R G E N C I A'
    →  'SINALIZACAO DE EMERGENCIA'

    Regra: ≥60% dos tokens são 1 char E ≥4 tokens.
    Duplos espaços separam palavras — simples espaços são espaçamento de letra.
    Preserva texto normal ('NT 21', 'RT 01') sem alteração.
    """
    tokens = text.split()
    if len(tokens) < 4:
        return text
    ratio = sum(1 for t in tokens if len(t) == 1) / len(tokens)
    if ratio < _SPACED_TEXT_RATIO:
        return text
    # Duplo espaço = limite de palavra; espaço simples = espaçamento de letra
    normalized = re.sub(r" {2,}", "\x00", text)   # marca limites de palavra
    normalized = normalized.replace(" ", "")        # colapsa espaçamento de letra
    return normalized.replace("\x00", " ").strip()  # restaura palavras

# Perfis de "qualidade" do DXF — ajustam side, margem e DPI alvo.
# - "alta":  DXF bem-organizado, escala normal.
# - "media": default. Compromisso entre legibilidade e contexto.
# - "baixa": DXF problemático (paper-space, layers caóticas). Mais margem,
#            render maior pra conseguir ler mesmo com escolha imprecisa de centro.
# Cada tupla é (side_multiplier, render_margin, target_px).
# Margens altas o suficiente para cobrir blocos gráficos (INSERT de símbolos,
# sinalizações) que ficam fora do bbox dos pontos de inserção de texto.
_QUALITY_PARAMS: dict[str, tuple[float, float, int]] = {
    "alta":  (0.8, 0.55, 4000),
    "media": (1.0, 0.60, 4500),
    "baixa": (2.0, 0.75, 6000),
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
    - poucos textos (<20): baixa — provavelmente blocos/xrefs não resolvidos
    - resto: media (a maioria dos casos)
    """
    if not info.extents:
        return "media", (
            "Não foi possível calcular as dimensões do arquivo (extents ausentes). "
            "A extração de clusters funcionará, mas sem garantia de precisão."
        )

    dx = info.extents[2] - info.extents[0]
    dy = info.extents[3] - info.extents[1]
    shorter = min(dx, dy)
    longer = max(dx, dy)
    aspect = longer / max(shorter, 0.001)
    n_texts = len(positions) if positions else info.n_texts

    # Sem textos legíveis: provavelmente tudo em blocos ou xrefs não resolvidos
    if n_texts < 20:
        return "baixa", (
            f"Apenas {n_texts} texto(s) encontrado(s) no arquivo. "
            "Provável causa: textos dentro de blocos ou referências externas (XREF) "
            "que o conversor não resolveu. Quadros, carimbos e legendas podem não aparecer."
        )

    # Paper space: coordenadas na escala da folha de papel (mm), não do projeto real
    if shorter < 500:
        return "baixa", (
            f"Arquivo em paper space (escala de impressão): a menor dimensão é "
            f"{shorter:.0f} unidades — equivalente a uma folha de papel em milímetros. "
            "As coordenadas não representam o modelo real, dificultando a localização "
            "de quadros e carimbos automaticamente."
        )

    # Planta muito estreita em relação ao comprimento
    if aspect > 30:
        return "baixa", (
            f"Planta muito alongada: o comprimento é {aspect:.0f}× a largura. "
            "Isso dispersa as informações ao longo de um único eixo e prejudica "
            "a identificação das regiões mais relevantes (quadros, RT, extintores)."
        )

    # Alta qualidade: modelo bem estruturado com proporções regulares e textos suficientes
    if shorter >= 5000 and aspect <= 5 and n_texts >= 200:
        return "alta", (
            f"Arquivo bem estruturado: planta com proporção {aspect:.1f}:1 (regular) "
            f"e {n_texts} textos identificados. "
            "Localização de quadros, carimbos e informações de PSCIP deve ser precisa."
        )

    # Media — justificativa específica conforme o(s) fator(es) limitante(s)
    limitantes = []
    if n_texts < 80:
        limitantes.append(
            f"quantidade de textos relativamente baixa ({n_texts} encontrados — "
            "ideal acima de 200 para projetos PSCIP completos)"
        )
    if aspect > 10:
        limitantes.append(
            f"planta alongada ({aspect:.0f}:1), o que pode dispersar as informações "
            "ao longo do comprimento"
        )
    if shorter < 2000:
        limitantes.append(
            f"escala reduzida (menor dimensão = {shorter:.0f} unidades) — "
            "pode indicar uso misto de model e paper space"
        )

    if limitantes:
        detalhe = "; ".join(limitantes)
        return "media", (
            f"Arquivo com qualidade intermediária: {detalhe}. "
            "A extração de clusters funciona, mas pode exigir revisão manual dos resultados."
        )

    return "media", (
        f"Arquivo com estrutura adequada: proporção {aspect:.1f}:1, {n_texts} textos. "
        "Extração de clusters funcional — recomenda-se conferir se os quadros de RT "
        "e legendas foram corretamente identificados."
    )

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
    # Cabeçalhos de tabela (ancoram o detector exatamente nos quadros)
    "QUADRO RESUMO", "MEDIDAS DE SEGURANÇA", "MEDIDAS DE SEGURANCA",
    "CARGA DE INCÊNDIO", "CARGA DE INCENDIO",
    "CLASSIFICAÇÃO", "CLASSIFICACAO",
    "OCUPAÇÃO", "OCUPACAO", "DIVISÃO", "DIVISAO", "GRUPO",
    "SEGURANÇA ESTRUTURAL", "SEGURANCA ESTRUTURAL",
    "POPULAÇÃO", "POPULACAO",
    "EDIFICAÇÃO", "EDIFICACAO",
    "CONFORME NORMA",   # aparece em ~toda linha do QUADRO RESUMO
]
# RT como token isolado (não confunde com palavras tipo "PARTE")
_RT_RE = re.compile(r"\bRT\b")
# Classificação de ocupação: A-1, M-3, F-8, etc.
_OCC_CLASS_RE = re.compile(r"\b[A-Z]-\d\b")
# Referência a Norma Técnica: NT 01/20, NT-14/20, NT 18, etc.
_NT_RE = re.compile(r"\bNT[\s\-]?\d")


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
    # Normaliza texto letra-espaçada antes de comparar keywords
    cleaned = _normalize_spaced(cleaned)
    if not cleaned:
        return 0.0
    score = 1.0
    for kw in _KEYWORDS_HIGH_VALUE:
        if kw in cleaned:
            score += 30.0   # era 10 — keyword vale muito mais que texto genérico
    if _RT_RE.search(cleaned):
        score += 30.0
    if _OCC_CLASS_RE.search(cleaned):
        score += 10.0
    if _NT_RE.search(cleaned):
        score += 15.0   # referência a Norma Técnica (NT 01/20, NT-14/20…)
    return score


def _find_top_clusters_scored(positions, n: int, side: float,
                               all_positions: list | None = None,
                               eps_hint: float | None = None):
    """
    Descobre BLOCOS visuais (tabelas, blocos de notas, quadros) e os
    rankeia por relevância de keywords PSCIP.

    Estratégia:
      1. DBSCAN em TODOS os textos (all_positions) com eps de escala de
         BLOCO — conecta linhas de uma tabela/parágrafo num cluster só,
         mas mantém blocos distintos separados. Assim o bbox engloba o
         quadro inteiro, não só o ponto onde a keyword aparece.
      2. Cada bloco é pontuado pela soma de _text_score dos seus membros
         (keywords pesam 30× mais). Blocos sem nenhuma keyword são
         descartados — só interessam regiões com conteúdo PSCIP.
      3. Top-N blocos por score; bbox = extensão real dos membros.
      4. Fallback para NMS nos keyword-matches se DBSCAN não produzir
         clusters suficientes.

    Args:
        positions:     textos com keywords (scoring + fallback).
        n:             quantos blocos retornar.
        side:          tamanho base da janela (do perfil de qualidade).
        all_positions: todos os textos do DXF — fonte do clustering de bloco.
        eps_hint:      raio de vizinhança sugerido (ex.: altura_texto × fator).
                       Se None, deriva da densidade de all_positions.
    """
    if not positions:
        return []

    try:
        from sklearn.cluster import DBSCAN as _DBSCAN
        import numpy as _np
        _has_sklearn = True
    except ImportError:
        _has_sklearn = False

    # Fonte do clustering: todos os textos (descobre blocos visuais).
    source = all_positions if (all_positions and len(all_positions) >= 4) else positions

    db_results: list[tuple] = []

    if _has_sklearn and len(source) >= 4:
        # eps de escala de BLOCO: grande o bastante para conectar linhas de
        # uma tabela/parágrafo, pequeno o bastante para não fundir blocos
        # distintos. Cap em 60% do side evita fusão da prancha inteira.
        if eps_hint and eps_hint > 0:
            eps = eps_hint
        else:
            eps = _density_side(source, k=4, factor=3.0)
        eps = min(max(eps, 1.0), side * 0.6)

        coords = _np.array([(p.x, p.y) for p in source])
        # min_samples=3: um bloco real tem ≥3 textos (linhas); pares isolados
        # viram ruído e não geram regiões espúrias.
        labels = _DBSCAN(eps=eps, min_samples=3).fit_predict(coords)

        db_clusters: dict[int, dict] = {}
        for pos, lbl in zip(source, labels):
            if lbl == -1:
                continue
            s = _text_score(pos.text)
            d = db_clusters.setdefault(
                lbl, {"members": [], "score": 0.0, "kw": 0})
            d["members"].append(pos)
            d["score"] += s
            if s > 1.0:
                d["kw"] += 1

        for data in db_clusters.values():
            # Só interessam blocos que contêm pelo menos uma keyword PSCIP.
            if data["kw"] < 1:
                continue
            members = data["members"]
            xs = [p.x for p in members]
            ys = [p.y for p in members]
            x_min, x_max = min(xs), max(xs)
            y_min, y_max = min(ys), max(ys)
            cx = (x_min + x_max) / 2
            cy = (y_min + y_max) / 2
            bbox_side = max(x_max - x_min, y_max - y_min, side * 0.10)
            c = Cluster(cx=cx, cy=cy, side=bbox_side, members=members)
            db_results.append((c, data["score"], data["kw"]))

        db_results.sort(key=lambda x: x[1], reverse=True)

    # Fallback: DBSCAN não achou blocos suficientes → NMS nos keyword-matches
    if len(db_results) < n:
        nms = _find_top_clusters_nms(positions, n, side)
        covered = [(c.cx, c.cy) for c, _, _ in db_results]
        for item in nms:
            c, sc, kw = item
            if not any(
                abs(c.cx - ux) <= side / 2 and abs(c.cy - uy) <= side / 2
                for ux, uy in covered
            ):
                db_results.append(item)
                covered.append((c.cx, c.cy))
            if len(db_results) >= n:
                break

    db_results.sort(key=lambda x: x[1], reverse=True)
    return db_results[:n]


def _find_top_clusters_nms(positions, n: int, side: float):
    """Fallback greedy NMS — usado quando sklearn não está disponível."""
    scored = [(p, _text_score(p.text)) for p in positions]
    half = side / 2
    remaining = list(scored)
    out = []

    for _ in range(n):
        if not remaining:
            break
        best_score, best_window = 0.0, []
        for tp, _s in remaining:
            window = [(p, s) for p, s in remaining
                      if abs(p.x - tp.x) <= half and abs(p.y - tp.y) <= half]
            ws = sum(s for _, s in window)
            if ws > best_score:
                best_score, best_window = ws, window
        if not best_window:
            break
        wx = sum(p.x for p, _ in best_window) / len(best_window)
        wy = sum(p.y for p, _ in best_window) / len(best_window)
        final = [(p, s) for p, s in remaining
                 if abs(p.x - wx) <= half and abs(p.y - wy) <= half]
        members = [p for p, _ in final]
        cx = sum(p.x for p in members) / len(members)
        cy = sum(p.y for p in members) / len(members)
        out.append((Cluster(cx=cx, cy=cy, side=side, members=members),
                    sum(s for _, s in final),
                    sum(1 for _, s in final if s > 1.0)))
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


def _text_height_dxf(msp, percentile: float = 0.20) -> float | None:
    """
    Retorna a altura de texto no percentil dado (padrão 20°) das entidades
    TEXT/MTEXT do modelspace — representa o texto de corpo (tabelas, notas),
    ignorando títulos grandes.

    Funciona em qualquer unidade DXF (mm, cm, metros, polegadas) porque
    usa a própria escala do arquivo.

    Retorna None se não houver textos com altura definida.
    """
    heights: list[float] = []
    for e in msp:
        try:
            if e.dxftype() == "TEXT":
                h = float(e.dxf.get("height", 0))
            elif e.dxftype() == "MTEXT":
                h = float(e.dxf.get("char_height", 0))
            else:
                continue
            if h > 0:
                heights.append(h)
        except Exception:
            continue
        if len(heights) >= 2000:
            break
    if len(heights) < 5:
        return None
    heights.sort()
    return heights[int(len(heights) * percentile)]


# Altura mínima do texto (em pixels) no PNG final para um LLM conseguir ler.
_DESIRED_TEXT_PX = 18


def _legible_side(msp, target_px: int, margin: float) -> float | None:
    """
    Calcula o `side` do cluster que garante que o texto de corpo do DXF
    terá pelo menos _DESIRED_TEXT_PX pixels de altura no PNG final.

    Derivação:
        render_window  = target_px  ×  text_height_dxf / _DESIRED_TEXT_PX
        cluster_side   = render_window / (1 + 2 × margin)

    Funciona em qualquer unidade DXF — a razão text_height/desired_px
    cancela a unidade automaticamente.
    """
    h = _text_height_dxf(msp)
    if h is None:
        return None
    render_window = target_px * h / _DESIRED_TEXT_PX
    return render_window / (1 + 2.0 * margin)


_CUT_BORDER_PX  = 6    # faixa de pixels da borda a inspecionar
_CUT_THRESHOLD  = 240  # pixel com canal mínimo < 240 é "conteúdo"
_CUT_EXPAND     = 1.5  # fator de expansão da margem a cada retry
_CUT_MAX_RETRY  = 2    # no máximo 2 tentativas extras após a primeira
# Limite superior: evita imagens gigantes em retries de clusters grandes.
_MAX_OUTPUT_PX  = 5500
# Limite inferior: garante legibilidade mesmo em clusters fisicamente pequenos.
# Sem esse floor, um cluster de 130 unidades a 400 DPI gera ~720px — ilegível.
_MIN_OUTPUT_PX  = 1200


def _is_cut(png_bytes: bytes) -> tuple[bool, list[str]]:
    """Detecta se o PNG tem conteúdo tocando as bordas (cluster cortado).

    Inspeciona uma faixa de _CUT_BORDER_PX pixels em cada lado. Se algum
    pixel tiver canal mínimo RGB < _CUT_THRESHOLD, considera cortado.
    Retorna (cortado, lista_de_lados_afetados).

    O PIL.Image.MAX_IMAGE_PIXELS é desativado localmente: os PNGs vêm do
    nosso próprio render (matplotlib), não de fontes externas.
    """
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit
    arr = np.array(img)
    dark = arr.min(axis=2)  # mínimo dos 3 canais; 0=preto, 255=branco
    b = _CUT_BORDER_PX
    lados: list[str] = []
    if dark[:b,  :].min()  < _CUT_THRESHOLD: lados.append("topo")
    if dark[-b:, :].min()  < _CUT_THRESHOLD: lados.append("base")
    if dark[:, :b].min()   < _CUT_THRESHOLD: lados.append("esquerda")
    if dark[:, -b:].min()  < _CUT_THRESHOLD: lados.append("direita")
    return bool(lados), lados


def _render_at(doc, cx: float, cy: float, rside: float, config,
               target_px: int = 4500) -> bytes | None:
    """
    Renderiza em (cx, cy) EXATOS — usado por /render-region.
    Não tenta recentralizar baseado em conteúdo; o ponto é a verdade.
    DPI adaptativo para manter ~target_px de lado.
    """
    dpi_floor = int(_MIN_OUTPUT_PX * 72 / rside)
    dpi_cap   = int(_MAX_OUTPUT_PX * 72 / rside)
    dpi = max(dpi_floor, min(int(target_px * 72 / rside), dpi_cap))
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
                    margin: float = 0.55, target_px: int = 4000,
                    max_retries: int = _CUT_MAX_RETRY,
                    bbox_cache: "ezdxf_bbox.Cache | None" = None) -> bytes | None:
    """
    Renderiza um cluster usando o bbox dos pontos de inserção + margem.

    Para qualidade ALTA/MÉDIA faz até max_retries tentativas extras
    expandindo a margem quando detecta conteúdo tocando a borda (blocos
    gráficos de sinalização/extintor que ficam fora dos insert points).
    Para qualidade BAIXA passe max_retries=0: cortes são esperados e
    tentar corrigir desperdiça tempo sem garantia de resultado.

    Não usa deslocamento de centro: o centróide dos membros garante
    espaço simétrico em todas as direções.

    bbox_cache: cache compartilhado entre todos os renders do mesmo arquivo.
        Evita recomputar bbox de blocos repetidos (INSERT) a cada tentativa.
        Passar None cria um cache local descartável por tentativa.
    """
    current_margin = margin
    last_png: bytes | None = None

    for attempt in range(max_retries + 1):
        rcx, rcy, rside, *_ = cluster.render_bounds(
            margin=current_margin, min_side=side
        )
        # DPI adaptativo: mantém entre _MIN_OUTPUT_PX e _MAX_OUTPUT_PX por lado.
        # Fórmula: px = rside/72 * dpi  →  dpi = px * 72 / rside
        dpi_ideal = int(target_px    * 72 / rside)
        dpi_cap   = int(_MAX_OUTPUT_PX * 72 / rside)
        dpi_floor = int(_MIN_OUTPUT_PX * 72 / rside)
        dpi = max(dpi_floor, min(dpi_ideal, dpi_cap))
        png_path = tempfile.mktemp(suffix=".png")
        try:
            ok = render_region(doc, rcx, rcy, rside, png_path,
                               dpi=dpi, config=config, verbose=False,
                               bbox_cache=bbox_cache)
            if not ok:
                return last_png
            with open(png_path, "rb") as f:
                png_bytes = f.read()
        finally:
            _cleanup(png_path)

        last_png = png_bytes
        if max_retries == 0:
            break  # BAIXA qualidade: não verifica corte, não tenta expandir
        cortado, _ = _is_cut(png_bytes)
        if not cortado:
            break
        current_margin *= _CUT_EXPAND

    return last_png


_RENDER_WORKERS = 4  # threads simultâneas por requisição


def _render_clusters_parallel(
    doc, clusters, side: float, config,
    margin: float, target_px: int, retries: int,
) -> dict[int, bytes | None]:
    """Renderiza clusters em paralelo. Retorna {idx_1based: png_bytes}."""
    def _worker(args):
        idx, cluster = args
        cache = ezdxf_bbox.Cache()  # cache isolado por thread
        return idx, _render_cluster(doc, cluster, side, config,
                                    margin=margin, target_px=target_px,
                                    max_retries=retries, bbox_cache=cache)

    n = min(len(clusters), _RENDER_WORKERS)
    results: dict[int, bytes | None] = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        for idx, png in pool.map(_worker, enumerate(clusters, 1)):
            results[idx] = png
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 1 — Renderizar região
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/render-region",
    summary="Renderiza região ao redor de (x, y) e retorna o PNG como download",
)
async def render_region_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
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
            if abs(p.x - x) <= rside and abs(p.y - y) <= rside
        ])
        if meaningful_nearby:
            max_off = max(
                max(abs(p.x - x) for p in meaningful_nearby),
                max(abs(p.y - y) for p in meaningful_nearby),
            )
            # cobre os textos vizinhos + extra: âncora do texto ≠ final do texto
            # fator 2.5 compensa largura de células de tabela (texto começa na esquerda)
            rside = max(rside, max_off * (2.5 + 2 * margin))

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
# Endpoint 1b — Render de um retângulo explícito (bbox)
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/render-bbox",
    summary="Renderiza exatamente o retângulo (x_min,y_min,x_max,y_max) e retorna o PNG",
)
async def render_bbox_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    x_min: Annotated[float, Query(description="Limite esquerdo do retângulo")],
    y_min: Annotated[float, Query(description="Limite inferior do retângulo")],
    x_max: Annotated[float, Query(description="Limite direito do retângulo")],
    y_max: Annotated[float, Query(description="Limite superior do retângulo")],
    pad: Annotated[
        float,
        Query(ge=0.0, le=1.0, description="Folga extra como fração do maior lado"),
    ] = 0.05,
    max_px: Annotated[
        int,
        Query(ge=500, le=8000, description="Lado máximo do PNG em pixels"),
    ] = 4000,
):
    """
    Recorta e renderiza exatamente o retângulo informado (em unidades DXF).

    Pensado para consumir o `rect`/`text_bbox` do /cluster-bounds: copie as
    4 coordenadas e renderize aquela região. `pad` adiciona uma folga extra
    (5% por padrão) para não cortar bordas de texto.
    """
    if x_max <= x_min or y_max <= y_min:
        raise HTTPException(
            status_code=422,
            detail="Retângulo inválido: exige x_max > x_min e y_max > y_min.",
        )

    dxf_path = _load_dxf_to_tmp(file)
    png_path: str | None = None

    try:
        doc, _ = recover.readfile(dxf_path)
        info = analyze_dxf(doc)
        color_policy = auto_detect_color_policy(info)

        # Aplica a folga proporcional ao maior lado do retângulo
        pad_abs = max(x_max - x_min, y_max - y_min) * pad
        region = (x_min - pad_abs, y_min - pad_abs,
                  x_max + pad_abs, y_max + pad_abs)

        side_ref = max(x_max - x_min, y_max - y_min)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side_ref, 200))

        png_path = tempfile.mktemp(suffix=".png")
        ok = render_overview_with_rects(
            doc, rects=[], output_path=png_path, region=region,
            max_px=max_px, config=config, verbose=False)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail="Região vazia — nenhuma entidade no retângulo informado.",
            )

        with open(png_path, "rb") as f:
            png_bytes = f.read()

        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "Content-Disposition": 'attachment; filename="bbox.png"',
            },
        )

    finally:
        _cleanup(dxf_path)
        if png_path:
            _cleanup(png_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 2 — Top clusters de texto
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/top-clusters",
    summary="Retorna os N clusters com mais textos na layer dominante",
    response_description="Lista de centros de cluster ordenados por densidade",
)
async def top_clusters_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
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
        side_mult, margin, target_px = _get_quality(qualidade)
        side = (_legible_side(msp, target_px, margin)
                or _smart_side(info, positions) * side_mult)
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
# Endpoint 2b — Coordenadas dos retângulos dos clusters PSCIP (sem renderizar)
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/cluster-bounds",
    summary="Retorna as coordenadas do retângulo de cada cluster PSCIP (sem renderizar)",
    response_description="Lista de retângulos (xmin,ymin,xmax,ymax) prontos para recorte",
)
async def cluster_bounds_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a retornar"),
    ] = 5,
):
    """
    Mesma lógica de detecção do /render-clusters-strings, mas em vez de
    renderizar PNGs, devolve as coordenadas do retângulo de cada cluster.

    Cada cluster traz:
      • rect       — retângulo de render (com margem) = janela que seria
                     impressa. Use estes 4 valores para recortar o desenho.
      • text_bbox  — bbox cru dos pontos de inserção dos textos (sem margem).
      • center, width, height, score, keyword_count, text_count.

    As coordenadas estão em unidades DXF (mesmo sistema de coordenadas do
    arquivo), prontas para alimentar um recorte ou um /render-region.
    """
    dxf_path = _load_dxf_to_tmp(file)

    try:
        tokens = {kw.upper() for kw in _KEYWORDS_HIGH_VALUE}

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            raise HTTPException(
                status_code=404,
                detail="Nenhum texto do DXF corresponde às keywords PSCIP.",
            )

        info = analyze_dxf(doc)
        all_positions = _filter_meaningful(collect_text_positions(msp, layer=None))
        qualidade, motivo = _detect_quality(
            info, all_positions if all_positions else matches)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = (_legible_side(msp, target_px, margin)
                or _smart_side(info, all_positions if all_positions else matches)
                * side_mult)

        scored = _find_top_clusters_scored(
            matches, n=n, side=side, all_positions=all_positions)

        clusters_out = []
        for c, total, kw in scored:
            # Mesma janela que _render_cluster usa (margem do perfil de qualidade)
            rcx, rcy, rside, xmin, ymin, xmax, ymax = c.render_bounds(
                margin=margin, min_side=side)
            xs = [p.x for p in c.members]
            ys = [p.y for p in c.members]
            clusters_out.append({
                "center": {"x": round(rcx, 4), "y": round(rcy, 4)},
                "rect": {
                    "x_min": round(xmin, 4),
                    "y_min": round(ymin, 4),
                    "x_max": round(xmax, 4),
                    "y_max": round(ymax, 4),
                },
                "width": round(xmax - xmin, 4),
                "height": round(ymax - ymin, 4),
                "text_bbox": {
                    "x_min": round(min(xs), 4),
                    "y_min": round(min(ys), 4),
                    "x_max": round(max(xs), 4),
                    "y_max": round(max(ys), 4),
                } if xs else None,
                "text_count": c.count,
                "keyword_count": kw,
                "score": round(total, 2),
            })

        return JSONResponse({
            "qualidade_dwg": qualidade,
            "qualidade_motivo": motivo,
            "side": round(side, 2),
            "margin": margin,
            "total_matches": len(matches),
            "clusters": clusters_out,
        })

    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint 2c — Overview do desenho com retângulos dos clusters plotados
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/plot-clusters",
    summary="Renderiza o desenho com os retângulos dos clusters PSCIP sobrepostos",
)
async def plot_clusters_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a plotar"),
    ] = 5,
    full: Annotated[
        bool,
        Query(description="True = desenho inteiro; False = só a área dos clusters"),
    ] = False,
    eps: Annotated[
        float | None,
        Query(description="Raio DBSCAN (unidades DXF). Maior = blocos maiores. "
                          "Vazio = automático pela densidade."),
    ] = None,
):
    """
    Visão geral para validação: renderiza o desenho e desenha por cima os
    retângulos coloridos de cada cluster PSCIP detectado (mesma lógica do
    /cluster-bounds). Retorna um único PNG.

    - full=False (padrão): enquadra apenas a região que engloba os clusters.
    - full=True: enquadra todo o extents do desenho (visão macro).
    """
    dxf_path = _load_dxf_to_tmp(file)
    png_path: str | None = None

    try:
        tokens = {kw.upper() for kw in _KEYWORDS_HIGH_VALUE}

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            raise HTTPException(
                status_code=404,
                detail="Nenhum texto do DXF corresponde às keywords PSCIP.",
            )

        info = analyze_dxf(doc)
        all_positions = _filter_meaningful(collect_text_positions(msp, layer=None))
        qualidade, _ = _detect_quality(
            info, all_positions if all_positions else matches)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = (_legible_side(msp, target_px, margin)
                or _smart_side(info, all_positions if all_positions else matches)
                * side_mult)
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side, 200))

        scored = _find_top_clusters_scored(
            matches, n=n, side=side, all_positions=all_positions, eps_hint=eps)
        if not scored:
            raise HTTPException(status_code=404,
                                detail="Nenhum cluster detectado.")

        rects: list[tuple[float, float, float, float]] = []
        labels: list[str] = []
        for i, (c, total, kw) in enumerate(scored, 1):
            _, _, _, xmin, ymin, xmax, ymax = c.render_bounds(
                margin=margin, min_side=side)
            rects.append((xmin, ymin, xmax, ymax))
            labels.append(f"{i} (kw={kw})")

        # full=True usa o extents do desenho como janela
        region = None
        if full and info.extents:
            region = info.extents

        png_path = tempfile.mktemp(suffix=".png")
        ok = render_overview_with_rects(
            doc, rects, png_path, labels=labels, region=region,
            config=config, verbose=False)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail="Região vazia — nada para renderizar.")

        with open(png_path, "rb") as f:
            png_bytes = f.read()

        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "Content-Disposition": 'attachment; filename="plot_clusters.png"',
                "X-Qualidade-DWG": qualidade,
                "X-Clusters": str(len(rects)),
            },
        )

    finally:
        _cleanup(dxf_path)
        if png_path:
            _cleanup(png_path)


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
            cleaned = _normalize_spaced(clean_mtext_preview(raw, max_len=500).upper())
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
    dxf: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
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

        clusters = [c for c, _, _ in _find_top_clusters_scored(matches, n=n, side=side)]

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
    dxf: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
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
        all_positions = _filter_meaningful(collect_text_positions(msp, layer=None))
        qualidade, _ = _detect_quality(info, all_positions if all_positions else matches)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = (_legible_side(msp, target_px, margin)
                or _smart_side(info, all_positions if all_positions else matches) * side_mult)
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side, 200))

        clusters = [c for c, _, _ in _find_top_clusters_scored(
            matches, n=n, side=side, all_positions=all_positions)]
        retries = 0 if qualidade == "baixa" else _CUT_MAX_RETRY

        rendered_map = _render_clusters_parallel(doc, clusters, side, config,
                                                 margin=margin, target_px=target_px,
                                                 retries=retries)

        zip_buf = io.BytesIO()
        rendered = 0
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, c in enumerate(clusters, 1):
                png_bytes = rendered_map.get(i)
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
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
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
# Endpoint 5 — Render de clusters PSCIP (keywords fixas)
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/render-clusters-strings",
    summary="Renderiza clusters PSCIP usando keywords fixas (RT, CREA, Extintor, etc.)",
)
async def render_clusters_strings_endpoint(
    dxf: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    n: Annotated[
        int,
        Query(ge=1, le=20, description="Quantidade de clusters a renderizar"),
    ] = 5,
):
    dxf_path = _load_dxf_to_tmp(dxf)

    try:
        # Usa o mesmo conjunto de keywords PSCIP do scoring de /top-clusters.
        # Já em UPPERCASE — _collect_csv_matches faz upper() no texto do DXF.
        tokens = {kw.upper() for kw in _KEYWORDS_HIGH_VALUE}

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        dominant_layer = find_dominant_text_layer(msp)

        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            raise HTTPException(status_code=404,
                                detail="Nenhum texto do DXF corresponde às keywords PSCIP.")

        info = analyze_dxf(doc)
        # Qualidade baseada em TODOS os textos (não só os matches),
        # para evitar "baixa" por ter poucos keywords num DXF rico.
        all_positions = _filter_meaningful(collect_text_positions(msp, layer=None))
        qualidade, _ = _detect_quality(info, all_positions if all_positions else matches)
        side_mult, margin, target_px = _get_quality(qualidade)

        # Side derivado da altura real do texto → janela calibrada para LLM.
        # Fallback para _smart_side se não conseguir medir altura.
        side = (_legible_side(msp, target_px, margin)
                or _smart_side(info, all_positions if all_positions else matches) * side_mult)
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side, 200))

        import logging as _lg
        _log = _lg.getLogger("api.strings")
        scored = _find_top_clusters_scored(matches, n=n, side=side,
                                           all_positions=all_positions)
        _log.warning(
            "[strings] qualidade=%s side=%.0f margin=%.2f matches=%d n_clusters=%d",
            qualidade, side, margin, len(matches), len(scored),
        )
        for i, (c, sc, kw) in enumerate(scored, 1):
            _log.warning(
                "  cluster %d: cx=%.0f cy=%.0f side=%.0f score=%.0f kw=%d members=%d",
                i, c.cx, c.cy, c.side, sc, kw, len(c.members),
            )
        clusters = [c for c, _, _ in scored]
        retries = 0 if qualidade == "baixa" else _CUT_MAX_RETRY

        rendered_map = _render_clusters_parallel(doc, clusters, side, config,
                                                 margin=margin, target_px=target_px,
                                                 retries=retries)

        zip_buf = io.BytesIO()
        rendered = 0
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, c in enumerate(clusters, 1):
                png_bytes = rendered_map.get(i)
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
# Pipeline isolado de extração de TABELAS (pasta table_pipeline/)
# Registrado por último para que todas as funcoes acima ja existam quando o
# router fizer import tardio de api.
# ─────────────────────────────────────────────────────────────────────────────
from table_pipeline import router as _table_router  # noqa: E402
app.include_router(_table_router)
