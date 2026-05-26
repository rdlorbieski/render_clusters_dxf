"""
render_text_clusters.py
─────────────────────────────────────────────────────────────────────────────
Detecta automaticamente as N regiões com maior densidade de texto num DXF e
renderiza cada uma como PNG em alta qualidade.

Algoritmo: greedy non-maximum suppression
  1. Coleta todas as posições de TEXT/MTEXT
  2. Para cada candidato, conta textos numa janela TAMANHO × TAMANHO centrada
     nele
  3. Pega a melhor janela, recentra no centroide, registra o cluster
  4. Remove os textos já cobertos
  5. Repete N vezes → clusters disjuntos

Reusa toda a lógica de renderização robusta do extract_region.py
(filtragem por bbox + ColorPolicy.COLOR_SWAP_BW + draw_entities + viewport
ajustado depois).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt

import ezdxf
from ezdxf import recover, bbox
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.config import (
    Configuration, BackgroundPolicy, ColorPolicy,
    LineweightPolicy, TextPolicy,
)


# ═════════════════════════════════════════════════════════════════════════════
# 🔧 CONFIGURAÇÃO
# ═════════════════════════════════════════════════════════════════════════════

DXF_FILE        = "PRJ2026006960.dxf"   # Arquivo DXF
N_CLUSTERS      = 3                     # Quantos clusters renderizar
TAMANHO         = 2000                  # Lado da região (unidades DXF)
DPI             = 200                   # Resolução
OUTPUT_PATTERN  = "cluster_{i}_n{n}.png"  # {i} = índice, {n} = qtd de textos

# Aparência
MONOCROMO       = False
BG_COLOR        = "#ffffff"
MIN_LINEWEIGHT  = 0.25
LINE_SCALING    = 1.0

# ═════════════════════════════════════════════════════════════════════════════


def collect_text_positions(msp):
    """Retorna [(x, y, texto)] de todas as TEXT/MTEXT com conteúdo."""
    out = []
    for e in msp:
        et = e.dxftype()
        if et not in ("TEXT", "MTEXT"):
            continue
        try:
            p = e.dxf.insert
            txt = e.dxf.text if et == "TEXT" else getattr(e.dxf, "text", "")
            if txt and txt.strip():
                out.append((p.x, p.y, txt))
        except Exception:
            pass
    return out


def find_top_clusters(positions, n_clusters, side):
    """
    Greedy NMS: encontra `n_clusters` janelas de lado `side` que maximizam
    a quantidade de textos cobertos, sem sobreposição entre clusters.
    Retorna lista de (cx, cy, n_textos, amostras).
    """
    half = side / 2.0
    remaining = list(positions)
    clusters = []

    for _ in range(n_clusters):
        if not remaining:
            break

        # Procura o ponto cuja janela centrada nele contém mais textos
        best_count = 0
        best_window = None
        for cx, cy, _ in remaining:
            window = [(x, y, t) for x, y, t in remaining
                      if abs(x - cx) <= half and abs(y - cy) <= half]
            if len(window) > best_count:
                best_count = len(window)
                best_window = window

        if not best_window:
            break

        # Recentra no centroide para janela mais "natural"
        wx = sum(x for x, _, _ in best_window) / len(best_window)
        wy = sum(y for _, y, _ in best_window) / len(best_window)
        final = [(x, y, t) for x, y, t in remaining
                 if abs(x - wx) <= half and abs(y - wy) <= half]

        clusters.append((wx, wy, len(final), final))

        # Remove textos já cobertos
        used = {(x, y) for x, y, _ in final}
        remaining = [p for p in remaining if (p[0], p[1]) not in used]

    return clusters


def filter_entities_by_bbox(msp, region):
    """Filtra entidades cujo bbox intersecta a região (rápido + correto)."""
    xmin, ymin, xmax, ymax = region
    cache = bbox.Cache()
    keep = []
    for e in msp:
        try:
            b = bbox.extents([e], fast=True, cache=cache)
            if b.has_data:
                mn, mx = b.extmin, b.extmax
                if not (mx.x < xmin or mn.x > xmax or
                        mx.y < ymin or mn.y > ymax):
                    keep.append(e)
            else:
                keep.append(e)
        except Exception:
            keep.append(e)
    return keep


def build_config():
    return Configuration(
        background_policy=BackgroundPolicy.WHITE,
        custom_bg_color=BG_COLOR,
        # COLOR_SWAP_BW: preserva cores ACI e inverte preto↔branco
        # (cor 7 "by-layer/white" → preto em fundo branco)
        color_policy=ColorPolicy.BLACK if MONOCROMO else ColorPolicy.COLOR_SWAP_BW,
        custom_fg_color="#000000",
        lineweight_policy=LineweightPolicy.RELATIVE,
        lineweight_scaling=LINE_SCALING,
        min_lineweight=MIN_LINEWEIGHT,
        text_policy=TextPolicy.FILLING,
        circle_approximation_count=128,
        max_flattening_distance=0.01,
    )


def render_region(doc, msp, cx, cy, side, output_path, label=""):
    """Renderiza uma região centrada em (cx, cy) com lado `side`."""
    half = side / 2.0
    xmin, xmax = cx - half, cx + half
    ymin, ymax = cy - half, cy + half
    region = (xmin, ymin, xmax, ymax)

    print(f"  [{label}] filtrando…", end=" ", flush=True)
    t0 = time.time()
    entidades = filter_entities_by_bbox(msp, region)
    print(f"{len(entidades)} entidades ({time.time()-t0:.1f}s)")

    if not entidades:
        print(f"  [{label}] ⚠️  vazio, pulando")
        return False

    print(f"  [{label}] renderizando…", end=" ", flush=True)
    t0 = time.time()

    fig_in = side / 72.0
    fig, ax = plt.subplots(figsize=(fig_in, fig_in))
    config = build_config()
    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend, config=config).draw_entities(entidades)

    # Limites DEPOIS de draw_entities (ver extract_region.py para o porquê)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.patch.set_facecolor(BG_COLOR)

    fig.savefig(output_path, dpi=DPI, bbox_inches=None, pad_inches=0,
                facecolor=BG_COLOR)
    plt.close(fig)

    size_kb = Path(output_path).stat().st_size / 1024
    print(f"{size_kb:.0f} KB ({time.time()-t0:.1f}s)")
    return True


def main():
    print("=" * 70)
    print(f"RENDER DOS {N_CLUSTERS} CLUSTERS DE TEXTO MAIS DENSOS")
    print("=" * 70)

    if not Path(DXF_FILE).exists():
        sys.exit(f"❌ Arquivo não encontrado: {DXF_FILE}")

    print(f"\n[1] Carregando {DXF_FILE}…")
    t0 = time.time()
    doc, _ = recover.readfile(DXF_FILE)
    msp = doc.modelspace()
    print(f"    ✓ {time.time() - t0:.1f}s")

    print(f"\n[2] Coletando posições de TEXT/MTEXT…")
    positions = collect_text_positions(msp)
    print(f"    ✓ {len(positions)} textos")

    print(f"\n[3] Achando {N_CLUSTERS} clusters (janelas {TAMANHO}×{TAMANHO})…")
    clusters = find_top_clusters(positions, N_CLUSTERS, TAMANHO)

    print(f"\n=== CLUSTERS ENCONTRADOS ===")
    for i, (cx, cy, n, samples) in enumerate(clusters, 1):
        print(f"\nCluster {i}: centro=({cx:.1f}, {cy:.1f}) — {n} textos")
        for x, y, t in samples[:3]:
            # Limpa códigos MTEXT óbvios para preview
            preview = (t.replace("\\P", " | ")
                        .replace("\\pxsm1;", "")
                        .replace("\\pxqc;", "")
                        .replace("{\\C0;", "")
                        .replace("}", "")
                        .strip())
            print(f"    {preview[:65]!r}")

    print(f"\n[4] Renderizando cada cluster…")
    saved = []
    for i, (cx, cy, n, _) in enumerate(clusters, 1):
        out = OUTPUT_PATTERN.format(i=i, n=n)
        print(f"\n  Cluster {i} → {out}")
        if render_region(doc, msp, cx, cy, TAMANHO, out, label=f"C{i}"):
            saved.append(out)

    print(f"\n[5] ✅ Concluído — {len(saved)} arquivos gerados:")
    for s in saved:
        print(f"    • {s}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()