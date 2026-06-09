"""
render_and_check.py
────────────────────────────────────────────────────────────────────────
Para cada DWG informado:
  1. Converte para DXF
  2. Avalia qualidade (baixa / media / alta)
  3. Renderiza os top clusters com keywords PSCIP
  4. Verifica se cada PNG foi cortado (conteúdo tocando a borda)
  5. Salva os PNGs em output/<nome_arquivo>/

Uso:
    python render_and_check.py arquivo1.dwg [arquivo2.dwg ...]
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
import tempfile
import shutil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import numpy as np
from PIL import Image

from ezdxf import recover
from converter import convert_dwg_to_dxf
from dxf_render import (
    analyze_dxf, auto_detect_color_policy, collect_text_positions,
    find_dominant_text_layer, find_top_clusters,
    build_config, suggest_lineweight,
)

# Importa helpers do api.py sem executar o servidor FastAPI
import importlib.util
_api_spec = importlib.util.spec_from_file_location("api_mod", Path(__file__).parent / "api.py")
_api_mod  = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(_api_mod)

_detect_quality        = _api_mod._detect_quality
_filter_meaningful     = _api_mod._filter_meaningful
_smart_side            = _api_mod._smart_side
_get_quality           = _api_mod._get_quality
_render_cluster        = _api_mod._render_cluster
_find_top_clusters_scored = _api_mod._find_top_clusters_scored
_KEYWORDS_HIGH_VALUE   = _api_mod._KEYWORDS_HIGH_VALUE
_collect_csv_matches   = _api_mod._collect_csv_matches
_cleanup               = _api_mod._cleanup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── detecção de corte ──────────────────────────────────────────────────────

_BG_THRESHOLD  = 240   # pixel com canal < 240 é considerado "conteúdo"
_BORDER_PIXELS = 6     # faixa de pixels da borda a inspecionar


def _is_cut(png_bytes: bytes, border: int = _BORDER_PIXELS,
            threshold: int = _BG_THRESHOLD) -> tuple[bool, list[str]]:
    """
    Verifica se o PNG tem conteúdo tocando a borda.

    Retorna (cortado, lados_afetados).
    Lógica: se alguma linha/coluna da faixa de borda tiver pixel com
    valor mínimo de canal < threshold, há conteúdo muito próximo da borda.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    arr = np.array(img)           # shape: (H, W, 3)
    # Mínimo dos canais RGB por pixel → 0 = preto puro, 255 = branco puro
    dark = arr.min(axis=2)        # shape: (H, W)

    cut_sides: list[str] = []
    if dark[:border, :].min()  < threshold: cut_sides.append("topo")
    if dark[-border:, :].min() < threshold: cut_sides.append("base")
    if dark[:, :border].min()  < threshold: cut_sides.append("esquerda")
    if dark[:, -border:].min() < threshold: cut_sides.append("direita")

    return bool(cut_sides), cut_sides


# ── render principal ───────────────────────────────────────────────────────

N_CLUSTERS = 5
OUTPUT_ROOT = Path("output_clusters")


def _process(dwg_path: Path) -> None:
    stem   = dwg_path.stem
    outdir = OUTPUT_ROOT / stem
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*72}")
    print(f"  {dwg_path.name}")
    print(f"{'═'*72}")

    tmp_dxf: str | None = None
    try:
        # 1. Conversão
        if dwg_path.suffix.lower() == ".dwg":
            print("  [1/4] Convertendo DWG → DXF…", end=" ", flush=True)
            tmp_dxf = str(convert_dwg_to_dxf(str(dwg_path)))
            print("OK")
        else:
            tmp_dxf = str(dwg_path)

        # 2. Leitura e análise
        print("  [2/4] Analisando…", end=" ", flush=True)
        doc, _ = recover.readfile(tmp_dxf)
        msp = doc.modelspace()
        info = analyze_dxf(doc)
        positions = collect_text_positions(msp, layer=None)
        positions = _filter_meaningful(positions)
        qualidade, motivo = _detect_quality(info, positions)
        side_mult, margin, target_px = _get_quality(qualidade)
        side = _smart_side(info, positions) * side_mult
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(side, 200))
        print("OK")

        print(f"\n  Qualidade: {'✅' if qualidade=='alta' else '🟡' if qualidade=='media' else '🔴'} "
              f"{qualidade.upper()}")
        print(f"  Motivo:    {motivo}")

        # 3. Clusters por keywords PSCIP
        print(f"\n  [3/4] Buscando clusters PSCIP (top {N_CLUSTERS})…", end=" ", flush=True)
        tokens = {kw.upper() for kw in _KEYWORDS_HIGH_VALUE}
        matches = _collect_csv_matches(msp, tokens)
        if not matches:
            # Fallback: usa todos os textos filtrados
            print("nenhum match de keyword — usando todos os textos como fallback")
            matches = positions

        scored = _find_top_clusters_scored(matches, n=N_CLUSTERS, side=side)
        print(f"{len(scored)} cluster(s) encontrado(s)")

        # Para BAIXA qualidade não faz retry — cortes são esperados
        retries = 0 if qualidade == "baixa" else _api_mod._CUT_MAX_RETRY

        # 4. Renderização + verificação de corte
        retry_label = f"(retry até {retries}×)" if retries else "(sem retry — qualidade baixa)"
        print(f"\n  [4/4] Renderizando e verificando cortes {retry_label}:")
        resultados = []
        for i, (cluster, total_score, kw_count) in enumerate(scored, 1):
            png_bytes = _render_cluster(doc, cluster, side, config,
                                        margin=margin, target_px=target_px,
                                        max_retries=retries)
            if png_bytes is None:
                print(f"    Cluster {i}: render vazio — ignorado")
                continue

            cortado, lados = _is_cut(png_bytes)
            status = f"⚠️  CORTADO ({', '.join(lados)})" if cortado else "✅ sem corte"

            fname = outdir / f"cluster_{i:02d}_score{total_score:.0f}_kw{kw_count}.png"
            fname.write_bytes(png_bytes)

            # Dimensões do PNG
            img = Image.open(io.BytesIO(png_bytes))
            w, h = img.size

            print(f"    Cluster {i}: {w}×{h}px | score={total_score:.0f} | "
                  f"kw={kw_count} | {status}")
            print(f"       → {fname.name}")

            resultados.append({
                "cluster": i, "cortado": cortado, "lados": lados,
                "score": total_score, "kw": kw_count, "file": fname,
            })

        cortes = [r for r in resultados if r["cortado"]]
        print(f"\n  Resumo: {len(resultados)} renderizados, "
              f"{len(cortes)} com corte detectado")
        if cortes:
            print(f"  ATENÇÃO: clusters com corte → "
                  + ", ".join(str(r['cluster']) for r in cortes))

    except Exception as exc:
        import traceback
        print(f"\n  ERRO: {exc}")
        traceback.print_exc()
    finally:
        if tmp_dxf and tmp_dxf != str(dwg_path):
            try:
                Path(tmp_dxf).unlink(missing_ok=True)
            except OSError:
                pass


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python render_and_check.py arquivo1.dwg [arquivo2.dwg ...]")
        sys.exit(1)

    arquivos = [Path(a) for a in sys.argv[1:]]
    for a in arquivos:
        if not a.exists():
            print(f"Não encontrado: {a}")
            sys.exit(1)

    OUTPUT_ROOT.mkdir(exist_ok=True)
    print(f"Saída em: {OUTPUT_ROOT.resolve()}")

    for arq in arquivos:
        _process(arq)

    print(f"\n{'═'*72}")
    print(f"Concluído. PNGs salvos em: {OUTPUT_ROOT.resolve()}")
    print(f"{'═'*72}\n")


if __name__ == "__main__":
    main()
