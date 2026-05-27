"""
dxf_render.py
═════════════════════════════════════════════════════════════════════════════
Biblioteca para extração e renderização de regiões de arquivos DXF como
imagens PNG em alta qualidade.

Resolve três problemas técnicos comuns ao usar o backend matplotlib do
`ezdxf.addons.drawing`:

  1. **Cor 7 ("by-layer" branco)**: em DXFs autorados no AutoCAD com
     fundo escuro, ~80–95% das entidades têm cor efetiva branca ou
     próxima de branca. Sem inversão, elas ficam invisíveis em fundo
     branco. → Detectado e corrigido automaticamente via amostragem.

  2. **`set_xlim`/`set_ylim` antes do `draw_layout` é sobrescrito** por
     `finalize=True` (que chama `autoscale_view`). → Sempre usar
     `draw_entities` e definir os limites DEPOIS.

  3. **Performance em arquivos grandes**: iterar 70k+ entidades é caro.
     → Pré-filtra por bounding box (via `ezdxf.bbox`, que entende blocos,
     splines, hatches etc.) antes de renderizar.

Função pública principal:
    render_region(doc, cx, cy, side, output_path, ...)

Funções auxiliares de alto nível:
    collect_text_positions(msp)         — extrai (x, y, texto) de TEXT/MTEXT
    find_top_clusters(positions, n, side) — top-N regiões com mais texto
    analyze_dxf(doc)                    — caracterização do arquivo
    auto_detect_color_policy(doc)       — escolhe COLOR vs COLOR_SWAP_BW
    suggest_region_size(doc)            — TAMANHO razoável dado o bbox
═════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NamedTuple

import matplotlib.pyplot as plt

import ezdxf
from ezdxf import bbox
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.config import (
    Configuration, BackgroundPolicy, ColorPolicy,
    LineweightPolicy, TextPolicy,
)


# ═════════════════════════════════════════════════════════════════════════════
# Tipos e estruturas
# ═════════════════════════════════════════════════════════════════════════════


class TextPos(NamedTuple):
    """Posição e conteúdo de uma entidade TEXT ou MTEXT.

    Attributes:
        x: Coordenada X do ponto de inserção (`entity.dxf.insert.x`).
        y: Coordenada Y do ponto de inserção.
        text: Conteúdo textual cru (pode conter códigos MTEXT como
            ``\\P``, ``\\C0;``, ``\\pxsm1;`` etc.).
    """
    x: float
    y: float
    text: str


@dataclass
class Cluster:
    """Um cluster (região) de textos contíguos.

    Attributes:
        cx: Coordenada X do centro da janela (centroide dos textos).
        cy: Coordenada Y do centro da janela.
        side: Lado da janela usada na detecção (em unidades DXF).
        members: Textos contidos no cluster.
    """
    cx: float
    cy: float
    side: float
    members: list[TextPos] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Número de textos no cluster."""
        return len(self.members)

    @property
    def region(self) -> tuple[float, float, float, float]:
        """Bounds da janela usada na detecção: ``(xmin, ymin, xmax, ymax)``."""
        h = self.side / 2.0
        return (self.cx - h, self.cy - h, self.cx + h, self.cy + h)

    def render_bounds(
        self,
        margin: float = 0.30,
        min_side: float = 0.0,
    ) -> tuple[float, float, float, float, float, float]:
        """Calcula bounds quadrados ideais para renderizar este cluster.

        Em vez de usar ``side`` fixo, computa o bbox real dos pontos de
        inserção dos textos e adiciona margem. Isso garante que:
          • Janelas pequenas não cortem textos longos (texto estende
            à direita do insert point).
          • Janelas grandes não desperdicem espaço com vazio.

        O resultado é forçado a quadrado (lado = maior dimensão).

        Args:
            margin: Fração do lado a adicionar como margem
                (0.30 = 30% extra em cada direção).
            min_side: Lado mínimo permitido (em unidades DXF). Evita
                que clusters de 1-2 textos virem janelas minúsculas.

        Returns:
            Tupla ``(cx, cy, side, xmin, ymin, xmax, ymax)`` — centro,
            lado e bounds da janela de renderização.
        """
        if not self.members:
            return (self.cx, self.cy, self.side,
                    *self.region)

        xs = [p.x for p in self.members]
        ys = [p.y for p in self.members]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        dx, dy = xmax - xmin, ymax - ymin
        base = max(dx, dy, min_side)
        side = base * (1.0 + 2 * margin)
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0
        h = side / 2.0
        return (cx, cy, side, cx - h, cy - h, cx + h, cy + h)


@dataclass
class DXFInfo:
    """Resumo das características de um arquivo DXF.

    Attributes:
        version: Versão do formato (ex.: ``"AC1032"``).
        units: Código ``$INSUNITS`` (4 = mm, 6 = m, 1 = in, ...).
        units_name: Nome humano do código de unidades.
        extents: Bounding box ``(xmin, ymin, xmax, ymax)`` do modelspace.
        size: Lado característico ``sqrt(dx * dy)`` em unidades DXF.
        n_entities: Total de entidades no modelspace.
        n_texts: Total de TEXT + MTEXT.
        pct_bright: Porcentagem de entidades cuja cor efetiva é clara
            (brightness > 200) — usada para decidir se inverter b/w.
    """
    version: str
    units: int
    units_name: str
    extents: tuple[float, float, float, float] | None
    size: float
    n_entities: int
    n_texts: int
    pct_bright: float


# ═════════════════════════════════════════════════════════════════════════════
# Coleta e clusterização de textos
# ═════════════════════════════════════════════════════════════════════════════


def find_dominant_text_layer(msp) -> str | None:
    """Retorna o nome da layer com mais entidades TEXT/MTEXT."""
    counts: dict[str, int] = {}
    for e in msp:
        if e.dxftype() in ("TEXT", "MTEXT"):
            layer = e.dxf.get("layer", "0")
            counts[layer] = counts.get(layer, 0) + 1
    return max(counts, key=counts.get) if counts else None


def collect_text_positions(msp, layer: str | None = None) -> list[TextPos]:
    """Extrai posições e conteúdos de todas as entidades TEXT/MTEXT.

    Itera o modelspace e coleta o ponto de inserção e o texto cru de cada
    entidade TEXT ou MTEXT que contenha conteúdo não-vazio. Entidades
    sem ``insert`` válido ou sem texto são ignoradas silenciosamente.

    Args:
        msp: Modelspace do DXF (resultado de ``doc.modelspace()``).
        layer: Se fornecido, filtra apenas entidades desta layer.

    Returns:
        Lista de :class:`TextPos`. Vazia se o DXF não tiver textos.

    Notes:
        O texto retornado é cru — inclui códigos de formatação MTEXT
        (``\\P``, ``\\pxsm1;``, ``\\C0;`` etc.). Para preview legível,
        use :func:`clean_mtext_preview`.
    """
    out: list[TextPos] = []
    for e in msp:
        et = e.dxftype()
        if et not in ("TEXT", "MTEXT"):
            continue
        if layer is not None and e.dxf.get("layer", "0") != layer:
            continue
        try:
            p = e.dxf.insert
            txt = e.dxf.text if et == "TEXT" else getattr(e.dxf, "text", "")
            if txt and txt.strip():
                out.append(TextPos(p.x, p.y, txt))
        except Exception:
            pass
    return out


def clean_mtext_preview(text: str, max_len: int = 60) -> str:
    """Remove códigos comuns de formatação MTEXT para exibição em log/console.

    Não é um parser completo — só remove os códigos mais frequentes para
    melhorar legibilidade em previews. O render real (via `ezdxf`) trata
    todos corretamente.

    Args:
        text: Texto cru, possivelmente com códigos MTEXT.
        max_len: Limite de caracteres no preview.

    Returns:
        Texto limpo, truncado em ``max_len`` caracteres.
    """
    cleaned = (text
               .replace("\\P", " | ")
               .replace("\\pxsm1;", "")
               .replace("\\pxqc;", "")
               .replace("{\\C0;", "")
               .replace("{\\W0.9;", "")
               .replace("}", "")
               .strip())
    return cleaned[:max_len]


def find_top_clusters(
    positions: list[TextPos],
    n_clusters: int,
    side: float,
) -> list[Cluster]:
    """Acha as N janelas quadradas mais densas em texto (greedy NMS).

    Algoritmo: para cada candidato, conta quantos textos caem na janela
    ``side × side`` centrada nele. Pega a melhor janela, recentra no
    centroide, registra como cluster, remove esses textos da pool e
    repete N vezes. Clusters retornados são disjuntos por construção.

    Complexidade: O(N² · k), com N = nº de textos e k = ``n_clusters``.
    Para N ≤ ~5k e k pequeno (≤ 10), roda em poucos segundos.

    Args:
        positions: Lista de :class:`TextPos` (saída de
            :func:`collect_text_positions`).
        n_clusters: Quantos clusters retornar. Se houver menos textos que
            isso suporta, retorna menos.
        side: Lado da janela quadrada (em unidades DXF).

    Returns:
        Lista de :class:`Cluster`, ordenada por densidade decrescente.

    Example:
        >>> textos = collect_text_positions(doc.modelspace())
        >>> clusters = find_top_clusters(textos, n_clusters=3, side=2000)
        >>> for c in clusters:
        ...     print(c.cx, c.cy, c.count)
    """
    half = side / 2.0
    remaining = list(positions)
    clusters: list[Cluster] = []

    for _ in range(n_clusters):
        if not remaining:
            break

        # Para cada ponto, conta textos na janela centrada nele
        best_count = 0
        best_window: list[TextPos] = []
        for tp in remaining:
            window = [
                p for p in remaining
                if abs(p.x - tp.x) <= half and abs(p.y - tp.y) <= half
            ]
            if len(window) > best_count:
                best_count = len(window)
                best_window = window

        if not best_window:
            break

        # Recentra no centroide (janela mais simétrica visualmente)
        wx = sum(p.x for p in best_window) / len(best_window)
        wy = sum(p.y for p in best_window) / len(best_window)
        final = [p for p in remaining
                 if abs(p.x - wx) <= half and abs(p.y - wy) <= half]

        clusters.append(Cluster(cx=wx, cy=wy, side=side, members=final))

        # Remove textos cobertos para garantir disjunção
        used = {(p.x, p.y) for p in final}
        remaining = [p for p in remaining if (p.x, p.y) not in used]

    return clusters


# ═════════════════════════════════════════════════════════════════════════════
# Filtragem de entidades por bbox
# ═════════════════════════════════════════════════════════════════════════════


def filter_entities_by_bbox(
    msp,
    region: tuple[float, float, float, float],
    layer: str | None = None,
):
    """Retorna apenas as entidades cuja bbox intersecta a região.

    Usa ``ezdxf.bbox.extents(..., fast=True)``, que conhece *todos* os
    tipos DXF (INSERT, BLOCK, SPLINE, HATCH, DIMENSION, LEADER, etc.) e
    resolve recursivamente blocos aninhados. Entidades sem bbox calculável
    (raro — ex.: proxy graphics) são incluídas por segurança.

    A flag ``fast=True`` usa heurísticas em vez de cálculos exatos
    (especialmente em splines e curvas), trocando precisão por velocidade.
    Para o uso de filtragem prévia isso é suficiente: o backend
    matplotlib clipa novamente depois.

    Args:
        msp: Modelspace do DXF.
        region: ``(xmin, ymin, xmax, ymax)`` em unidades DXF.
        layer: Se fornecido, inclui apenas entidades desta layer.

    Returns:
        Lista de entidades DXF cuja bounding box intersecta ``region``.

    Performance:
        70.000 entidades → ~6s. Cache interno acelera bbox de blocos
        repetidos (INSERTs).
    """
    xmin, ymin, xmax, ymax = region
    cache = bbox.Cache()
    keep = []
    for e in msp:
        if layer is not None and e.dxf.get("layer", "0") != layer:
            continue
        try:
            b = bbox.extents([e], fast=True, cache=cache)
            if b.has_data:
                mn, mx = b.extmin, b.extmax
                if not (mx.x < xmin or mn.x > xmax or
                        mx.y < ymin or mn.y > ymax):
                    keep.append(e)
            else:
                keep.append(e)  # sem bbox → mantém por segurança
        except Exception:
            keep.append(e)
    return keep


# ═════════════════════════════════════════════════════════════════════════════
# Auto-detecção
# ═════════════════════════════════════════════════════════════════════════════


_UNIT_NAMES = {
    0: "adimensional", 1: "polegadas", 2: "pés", 3: "milhas",
    4: "milímetros", 5: "centímetros", 6: "metros", 7: "quilômetros",
}


def analyze_dxf(doc, sample_size: int = 2000) -> DXFInfo:
    """Caracteriza um DXF: versão, unidades, extensão, densidade de cores.

    Usa amostragem aleatória (seed fixa para reprodutibilidade) para
    estimar a proporção de cores claras. O ``RenderContext`` resolve
    BYLAYER/BYBLOCK até a cor RGB final.

    Args:
        doc: Documento DXF (``ezdxf.recover.readfile(...)[0]``).
        sample_size: Tamanho da amostra para cálculo de ``pct_bright``.
            Aumentar dá mais precisão e custa mais tempo.

    Returns:
        :class:`DXFInfo` populado.

    Notes:
        Brightness = média (R+G+B)/3. Limite de 200 escolhido
        empiricamente: separa bem branco/cinza-claro de cores médias.
    """
    msp = doc.modelspace()

    # Extensão
    try:
        b = bbox.extents(msp, fast=True)
        if b.has_data:
            ext = (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)
            dx = ext[2] - ext[0]
            dy = ext[3] - ext[1]
            size = (dx * dy) ** 0.5
        else:
            ext, size = None, 0.0
    except Exception:
        ext, size = None, 0.0

    # Contagens básicas
    n_entities = len(msp)
    n_texts = sum(1 for e in msp if e.dxftype() in ("TEXT", "MTEXT"))

    # Amostragem de cor efetiva
    ctx = RenderContext(doc)
    all_ents = list(msp)
    random.seed(42)
    sample = random.sample(all_ents, min(sample_size, len(all_ents))) \
        if all_ents else []

    bright = total = 0
    for e in sample:
        try:
            props = ctx.resolve_all(e)
            c = props.color  # "#RRGGBB" ou "#RRGGBBAA"
            r = int(c[1:3], 16); g = int(c[3:5], 16); b_ = int(c[5:7], 16)
            if (r + g + b_) / 3 > 200:
                bright += 1
            total += 1
        except Exception:
            pass

    pct_bright = (100.0 * bright / total) if total else 0.0
    units = doc.header.get("$INSUNITS", 0)

    return DXFInfo(
        version=doc.dxfversion,
        units=units,
        units_name=_UNIT_NAMES.get(units, "?"),
        extents=ext,
        size=size,
        n_entities=n_entities,
        n_texts=n_texts,
        pct_bright=pct_bright,
    )


def auto_detect_color_policy(
    info: DXFInfo,
    threshold_pct: float = 50.0,
) -> ColorPolicy:
    """Escolhe automaticamente entre ``COLOR`` e ``COLOR_SWAP_BW``.

    Se a maioria das entidades tem cor efetiva clara, o autor presumiu
    fundo escuro do AutoCAD → precisa swap para visibilidade em fundo
    branco. Caso contrário, usa cores como estão.

    Args:
        info: Resultado de :func:`analyze_dxf`.
        threshold_pct: Acima desta % de entidades claras, ativa SWAP_BW.
            Default 50% é conservador; reduza para casos limítrofes.

    Returns:
        ``ColorPolicy.COLOR_SWAP_BW`` ou ``ColorPolicy.COLOR``.

    Notes:
        ``COLOR_SWAP_BW`` preserva todas as cores ACI específicas (vermelho,
        verde, azul…). Só preto↔branco são trocados, então labels coloridos
        ficam intactos.
    """
    return (ColorPolicy.COLOR_SWAP_BW
            if info.pct_bright > threshold_pct
            else ColorPolicy.COLOR)


def suggest_region_size(info: DXFInfo, target_windows: int = 10) -> float:
    """Sugere um TAMANHO razoável dado o bbox global.

    Heurística: ``sqrt(dx * dy) / target_windows``. Para um terreno de
    26 × 12 km, com ``target_windows=10``, sugere ~1820 m — escala
    apropriada pra ler texto técnico. Mais janelas = mais zoom in.

    Args:
        info: Resultado de :func:`analyze_dxf`.
        target_windows: Quantos "lados de janela" cabem na diagonal
            característica do desenho. ~6–10 funciona bem para a maioria
            dos casos. Para uma vista geral do desenho inteiro use 1.

    Returns:
        Tamanho sugerido em unidades DXF (float). Retorna 1000.0 se não
        houver extents válidos.

    Notes:
        Para **clusters de texto**, prefira usar ``Cluster.render_bounds()``,
        que dimensiona cada janela pelo bbox real dos seus membros — mais
        robusto do que um TAMANHO fixo global.
    """
    if info.extents is None:
        return 1000.0
    dx = info.extents[2] - info.extents[0]
    dy = info.extents[3] - info.extents[1]
    return (dx * dy) ** 0.5 / target_windows


def suggest_lineweight(side: float, dpi: int) -> float:
    """Sugere ``min_lineweight`` (em pt) pra garantir linhas visíveis.

    Em rendering técnico, queremos linhas com >= 0.7 px no PNG final.
    O cálculo: pixels_per_pt = dpi / 72. Então 1 pt → ``dpi/72`` px.
    Para >= 0.7 px, ``min_lw_pt = 0.7 * 72 / dpi``.

    Args:
        side: Lado da região em unidades DXF (não usado diretamente,
            mantido para futuras extensões dependentes de escala).
        dpi: Resolução de saída.

    Returns:
        Espessura mínima em pontos tipográficos (input de
        ``Configuration.min_lineweight``).
    """
    return max(0.15, 0.7 * 72.0 / dpi)


# ═════════════════════════════════════════════════════════════════════════════
# Renderização
# ═════════════════════════════════════════════════════════════════════════════


def build_config(
    color_policy: ColorPolicy = ColorPolicy.COLOR_SWAP_BW,
    bg_color: str = "#ffffff",
    min_lineweight: float = 0.25,
    lineweight_scaling: float = 1.0,
) -> Configuration:
    """Monta a ``Configuration`` do renderizador ezdxf.

    Args:
        color_policy: Política de cor — use
            :func:`auto_detect_color_policy` para escolher automaticamente.
        bg_color: Cor de fundo em hex (``"#ffffff"`` default).
        min_lineweight: Espessura mínima em pt — use
            :func:`suggest_lineweight` para valor automático.
        lineweight_scaling: Multiplicador global de espessura.

    Returns:
        :class:`Configuration` pronta para passar ao ``Frontend``.

    Notes:
        Outras opções (qualidade de aproximação de círculos, política de
        hatch etc.) ficam no default do ezdxf, que é robusto.
    """
    return Configuration(
        background_policy=(BackgroundPolicy.WHITE
                           if bg_color.lower() in ("#ffffff", "white", "#fff")
                           else BackgroundPolicy.CUSTOM),
        custom_bg_color=bg_color,
        color_policy=color_policy,
        custom_fg_color="#000000",
        lineweight_policy=LineweightPolicy.RELATIVE,
        lineweight_scaling=lineweight_scaling,
        min_lineweight=min_lineweight,
        text_policy=TextPolicy.FILLING,
        circle_approximation_count=128,
        max_flattening_distance=0.01,
    )


def render_region(
    doc,
    cx: float,
    cy: float,
    side: float,
    output_path: str | Path,
    *,
    dpi: int = 200,
    config: Configuration | None = None,
    bg_color: str = "#ffffff",
    layer: str | None = None,
    verbose: bool = True,
) -> bool:
    """Renderiza uma região quadrada do DXF como PNG em alta qualidade.

    Faz três coisas, na ordem:

      1. Filtra entidades cuja bbox intersecta a região
         (:func:`filter_entities_by_bbox`).
      2. Renderiza com ``Frontend.draw_entities`` (NÃO ``draw_layout``,
         pra evitar autoscale).
      3. Aplica ``set_xlim``/``set_ylim`` DEPOIS, garantindo que o
         viewport seja exatamente a região pedida.

    Args:
        doc: Documento DXF (``ezdxf.recover.readfile(...)[0]``).
        cx: Coordenada X do centro da região.
        cy: Coordenada Y do centro da região.
        side: Lado do quadrado (em unidades DXF).
        output_path: Caminho do PNG de saída.
        dpi: Resolução de saída. Pixel size = ``side / 72 * dpi``.
            Default 200 dá ~5500 × 5500 px para uma região de 2000 unidades.
        config: ``Configuration`` customizada. Se None, usa
            :func:`build_config` com defaults razoáveis.
        bg_color: Cor de fundo (usada se ``config`` é None).
        verbose: Imprime progresso/timing se True.

    Returns:
        True se renderizou e salvou; False se a região estava vazia.

    Example:
        >>> doc, _ = ezdxf.recover.readfile("plant.dxf")
        >>> render_region(doc, 346451.4, 7697478.9, 2000, "out.png", dpi=300)
        True
    """
    output_path = Path(output_path)
    half = side / 2.0
    region = (cx - half, cy - half, cx + half, cy + half)

    if verbose:
        print(f"    filtrando…", end=" ", flush=True)
    t0 = time.time()
    entidades = filter_entities_by_bbox(doc.modelspace(), region, layer=layer)
    if verbose:
        print(f"{len(entidades)} entidades ({time.time()-t0:.1f}s)")

    if not entidades:
        if verbose:
            print(f"    ⚠️  região vazia")
        return False

    if config is None:
        config = build_config(bg_color=bg_color)

    if verbose:
        print(f"    renderizando…", end=" ", flush=True)
    t0 = time.time()

    # figsize em polegadas: 1 unidade DXF = 1 pt = 1/72 polegada.
    # px_total = side/72 * dpi
    fig_in = side / 72.0
    fig, ax = plt.subplots(figsize=(fig_in, fig_in))

    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend, config=config).draw_entities(entidades)

    # CRÍTICO: limites DEPOIS de draw_entities (não draw_layout!)
    ax.set_xlim(region[0], region[2])
    ax.set_ylim(region[1], region[3])
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.patch.set_facecolor(bg_color)

    fig.savefig(output_path, dpi=dpi, bbox_inches=None, pad_inches=0,
                facecolor=bg_color)
    plt.close(fig)

    if verbose:
        size_kb = output_path.stat().st_size / 1024
        print(f"{size_kb:.0f} KB ({time.time()-t0:.1f}s)")
    return True
