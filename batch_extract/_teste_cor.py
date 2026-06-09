"""Teste pontual da correção de cor (ColorPolicy.BLACK no render de tabelas).

Confirma:
  • Igreja: a tabela do QUADRO DE ÁREAS agora mostra texto (mais pixels escuros
    com BLACK do que com a policy auto-detect antiga).
  • Comércio: continua legível (não-regressão).

Salva os PNGs em batch_extract/_teste_cor/ para inspeção visual.
"""
from __future__ import annotations
import matplotlib; matplotlib.use("Agg")
import io, os, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import logging; logging.basicConfig(level=logging.ERROR)

import numpy as np
from PIL import Image
from ezdxf import recover
from converter import convert_dwg_to_dxf
from dxf_render import (analyze_dxf, auto_detect_color_policy, build_config,
                        suggest_lineweight, render_overview_with_rects)
from ezdxf.addons.drawing.config import ColorPolicy
from table_pipeline.pipeline import run_pipeline, render_tables

OUT = Path(__file__).resolve().parent / "_teste_cor"
OUT.mkdir(exist_ok=True)

IGREJA = r"C:\Users\Usuario\Downloads\Pacote de Projetos PPCI\Pacote de Projetos PPCI\Igreja\PJ_PPCI_IGREJA PRESBITERIANA BETANIA-GYN_R01 - 2.771.95M2.dwg"
COMERCIO = r"C:\Users\Usuario\Downloads\Pacote de Projetos PPCI\Pacote de Projetos PPCI\Comercio\PSCIP_ALTO NORTE SEMENTES_FORMOSA-GOIAS_R01 - 1.441.44M2.dwg"


def _dark_frac(png: bytes) -> float:
    """Fração de pixels escuros (conteúdo) — proxy de 'tem texto/linhas'."""
    arr = np.array(Image.open(io.BytesIO(png)).convert("L"))
    return float((arr < 128).mean())


def _load(path):
    p = Path(path)
    if p.suffix.lower() == ".dwg":
        dxf = str(convert_dwg_to_dxf(str(p)))
        doc, _ = recover.readfile(dxf)
        return doc, dxf
    doc, _ = recover.readfile(str(p))
    return doc, None


def _processa(path, tag):
    print(f"\n=== {tag} ===")
    doc, tmp = _load(path)
    try:
        result = run_pipeline(doc, n=5)
        if result.aborted:
            print(f"  qualidade baixa: {result.motivo[:60]}")
            return
        rendered = render_tables(doc, result)   # já usa ColorPolicy.BLACK
        outdir = OUT / tag; outdir.mkdir(exist_ok=True)
        for r in rendered:
            (outdir / r["name"]).write_bytes(r["png"])
            print(f"  [BLACK] {r['name']:42} dark={_dark_frac(r['png'])*100:5.2f}%  "
                  f"kws={', '.join(r['keywords'][:2])}")

        # Comparação só p/ Igreja: re-render da tabela de áreas com policy antiga
        if tag == "igreja":
            area = next((t for t in result.tables
                         if any("AREA" in k.upper() or "ÁREA" in k.upper()
                                for k in t.keywords)), None)
            if area:
                info = analyze_dxf(doc)
                old_policy = auto_detect_color_policy(info)
                x0, y0, x1, y1 = area.bbox
                pad = result.cell * 1.5
                region = (x0-pad, y0-pad, x1+pad, y1+pad)
                cfg = build_config(color_policy=old_policy,
                                   min_lineweight=suggest_lineweight(
                                       max(area.width, area.height, result.cell), 200))
                png_old = OUT / tag / "_AREAS_policy_ANTIGA.png"
                render_overview_with_rects(doc, rects=[], output_path=str(png_old),
                                           region=region, dpi=300, config=cfg)
                old = png_old.read_bytes()
                print(f"\n  >>> COMPARACAO tabela de AREAS:")
                print(f"      policy ANTIGA ({old_policy.name}): dark={_dark_frac(old)*100:5.2f}%")
    finally:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass


_processa(IGREJA, "igreja")
_processa(COMERCIO, "comercio")
print(f"\nPNGs em: {OUT}")
