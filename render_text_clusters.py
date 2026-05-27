"""
render_text_clusters.py
─────────────────────────────────────────────────────────────────────────────
Detecta automaticamente as N regiões com maior densidade de texto num DXF
e renderiza cada uma como PNG em alta qualidade.

TUDO é auto-detectado quando AUTO=True:
  • Política de cor (COLOR vs COLOR_SWAP_BW) — amostragem de cor efetiva
  • Espessura mínima de linha — baseada no DPI
  • TAMANHO da janela — fração do bbox global (override possível)

A lógica reusável vive em `dxf_render.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ezdxf import recover

from dxf_render import (
    analyze_dxf, auto_detect_color_policy, suggest_region_size,
    suggest_lineweight, build_config,
    collect_text_positions, find_top_clusters, find_dominant_text_layer,
    clean_mtext_preview, render_region,
)


# ═════════════════════════════════════════════════════════════════════════════
# 🔧 CONFIGURAÇÃO
# ═════════════════════════════════════════════════════════════════════════════

DXF_FILE   = "PRJ2026006960.dxf"     # Arquivo DXF
N_CLUSTERS = 3                       # Quantos clusters renderizar
DPI        = 200                     # Resolução
OUTPUT_FMT = "cluster_{i}_n{n}.png"  # {i}=índice, {n}=qtd textos

# Use AUTO=True pra deixar o script decidir tudo, ou False pra manual
AUTO       = True
# Overrides (só usados se AUTO=False — ou pra forçar algum valor)
TAMANHO_MANUAL      = 2000           # Lado da janela em unidades DXF
COLOR_POLICY_MANUAL = None           # ex.: ColorPolicy.COLOR
MIN_LW_MANUAL       = None           # ex.: 0.25

# ═════════════════════════════════════════════════════════════════════════════


def main() -> None:
    if not Path(DXF_FILE).exists():
        sys.exit(f"❌ Arquivo não encontrado: {DXF_FILE}")

    print("=" * 70)
    print(f"RENDER DOS {N_CLUSTERS} CLUSTERS DE TEXTO MAIS DENSOS")
    print("=" * 70)

    print(f"\n[1] Carregando {DXF_FILE}…")
    t0 = time.time()
    doc, _ = recover.readfile(DXF_FILE)
    print(f"    ✓ {time.time() - t0:.1f}s")

    # --- auto-detecção ----------------------------------------------------
    print(f"\n[2] Analisando arquivo…")
    info = analyze_dxf(doc)
    print(f"    Versão DXF:       {info.version}")
    print(f"    Unidades:         {info.units_name} (código {info.units})")
    if info.extents:
        x0, y0, x1, y1 = info.extents
        print(f"    Extensão:         {x1-x0:.0f} × {y1-y0:.0f} unidades")
    print(f"    Entidades:        {info.n_entities}")
    print(f"    TEXT/MTEXT:       {info.n_texts}")
    print(f"    % cores claras:   {info.pct_bright:.1f}%")

    if AUTO:
        color_policy = auto_detect_color_policy(info)
        size = suggest_region_size(info)
        min_lw = suggest_lineweight(size, DPI)
        print(f"\n    Auto-detectado:")
        print(f"      cor:         {color_policy.name}")
        print(f"      TAMANHO:     {size:.0f}")
        print(f"      min_lw:      {min_lw:.3f}")
    else:
        color_policy = COLOR_POLICY_MANUAL or auto_detect_color_policy(info)
        size = TAMANHO_MANUAL
        min_lw = MIN_LW_MANUAL or suggest_lineweight(size, DPI)
        print(f"\n    Manual:")
        print(f"      cor:         {color_policy.name}")
        print(f"      TAMANHO:     {size}")
        print(f"      min_lw:      {min_lw:.3f}")

    # --- cluster detection ------------------------------------------------
    print(f"\n[3] Coletando textos…")
    positions = collect_text_positions(doc.modelspace())
    print(f"    ✓ {len(positions)} textos com posição válida")

    print(f"\n[4] Achando {N_CLUSTERS} clusters (janelas {size:.0f}×{size:.0f})…")
    clusters = find_top_clusters(positions, N_CLUSTERS, side=size)

    print(f"\n=== CLUSTERS ENCONTRADOS ===")
    for i, c in enumerate(clusters, 1):
        print(f"\nCluster {i}: centro=({c.cx:.1f}, {c.cy:.1f}) — {c.count} textos")
        for tp in c.members[:3]:
            print(f"    {clean_mtext_preview(tp.text, 65)!r}")

    # --- render -----------------------------------------------------------
    config = build_config(color_policy=color_policy, min_lineweight=min_lw)

    print(f"\n[5] Renderizando…")
    saved: list[str] = []
    for i, c in enumerate(clusters, 1):
        # render_bounds dimensiona a janela pelo bbox real dos membros
        # do cluster + 30% de margem (vs. tamanho fixo `c.side`), assim
        # textos longos não são cortados nem ficam com vazio em volta.
        rcx, rcy, rside, *_ = c.render_bounds(margin=0.30,
                                              min_side=size * 0.5)
        out = OUTPUT_FMT.format(i=i, n=c.count)
        print(f"\n  Cluster {i} → {out}  (lado {rside:.0f})")
        if render_region(doc, rcx, rcy, rside, out,
                         dpi=DPI, config=config):
            saved.append(out)

    print(f"\n[6] ✅ {len(saved)} arquivos gerados:")
    for s in saved:
        print(f"    • {s}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
