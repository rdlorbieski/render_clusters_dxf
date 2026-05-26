"""
extract_region.py
─────────────────────────────────────────────────────────────────────────────
Renderiza UMA região específica de um DXF como PNG em alta qualidade.

Lógica robusta de rendering (filtragem de bbox + COLOR_SWAP_BW automático
+ viewport ajustado depois) vem do módulo `dxf_render.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ezdxf import recover

from dxf_render import (
    analyze_dxf, auto_detect_color_policy,
    suggest_lineweight, build_config, render_region,
)


# ═════════════════════════════════════════════════════════════════════════════
# 🔧 CONFIGURAÇÃO
# ═════════════════════════════════════════════════════════════════════════════

DXF_FILE = "PRJ2026006960.dxf"
X_CENTRO = 346451.4
Y_CENTRO = 7697478.9
TAMANHO  = 2000
DPI      = 200
OUTPUT   = "regiao_hq.png"

# AUTO=True usa auto-detecção pra cor/espessura. False → use os valores
# em MIN_LINEWEIGHT_MANUAL e COLOR_POLICY_MANUAL abaixo.
AUTO                  = True
MIN_LINEWEIGHT_MANUAL = 0.25
COLOR_POLICY_MANUAL   = None  # None → auto-detect mesmo com AUTO=False

# ═════════════════════════════════════════════════════════════════════════════


def main() -> None:
    if not Path(DXF_FILE).exists():
        sys.exit(f"❌ Arquivo não encontrado: {DXF_FILE}")

    print("=" * 70)
    print("EXTRATOR DE REGIÃO DXF")
    print("=" * 70)

    print(f"\n[1] Carregando {DXF_FILE}…")
    t0 = time.time()
    doc, _ = recover.readfile(DXF_FILE)
    print(f"    ✓ {time.time() - t0:.1f}s")

    print(f"\n[2] Analisando arquivo…")
    info = analyze_dxf(doc)
    print(f"    Versão {info.version} · {info.n_entities} entidades · "
          f"{info.n_texts} textos · {info.pct_bright:.0f}% cores claras")

    if AUTO:
        color_policy = auto_detect_color_policy(info)
        min_lw = suggest_lineweight(TAMANHO, DPI)
        print(f"    Auto: cor={color_policy.name}, min_lw={min_lw:.3f}")
    else:
        color_policy = COLOR_POLICY_MANUAL or auto_detect_color_policy(info)
        min_lw = MIN_LINEWEIGHT_MANUAL
        print(f"    Manual: cor={color_policy.name}, min_lw={min_lw:.3f}")

    print(f"\n[3] Região alvo:")
    print(f"    Centro: ({X_CENTRO}, {Y_CENTRO})")
    print(f"    Lado:   {TAMANHO} unidades · saída ~{int(TAMANHO/72*DPI)} px")

    print(f"\n[4] Renderizando…")
    config = build_config(color_policy=color_policy, min_lineweight=min_lw)
    ok = render_region(doc, X_CENTRO, Y_CENTRO, TAMANHO, OUTPUT,
                       dpi=DPI, config=config)

    print(f"\n[5] {'✅ Concluído' if ok else '❌ Região vazia'}: {OUTPUT}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
