"""
analyze_quality.py
──────────────────
Roda a avaliação de qualidade de DWG/DXF nos arquivos informados na linha
de comando e imprime um relatório legível.

Uso:
    python analyze_quality.py arquivo1.dwg arquivo2.dxf ...
"""
from __future__ import annotations

import sys
import io
import re
from pathlib import Path

# Força UTF-8 no stdout para evitar erros de encoding no Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Garante que o matplotlib não tente abrir janela
import matplotlib
matplotlib.use("Agg")

from ezdxf import recover
from converter import convert_dwg_to_dxf
from dxf_render import analyze_dxf, collect_text_positions, find_dominant_text_layer

# ── imports internos do api.py ─────────────────────────────────────────────
# Importamos as funções diretamente para evitar duplicação
import importlib.util, types

_api_spec = importlib.util.spec_from_file_location("api_mod", Path(__file__).parent / "api.py")
_api_mod  = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(_api_mod)

_detect_quality   = _api_mod._detect_quality
_filter_meaningful = _api_mod._filter_meaningful
_QUALITY_LABELS = {"alta": "ALTA", "media": "MÉDIA", "baixa": "BAIXA"}
_QUALITY_ICONS  = {"alta": "✅", "media": "🟡", "baixa": "🔴"}


def _analyze_file(path: Path) -> None:
    print(f"\n{'═'*72}")
    print(f"  Arquivo: {path.name}")
    print(f"{'═'*72}")

    tmp_dxf: str | None = None
    try:
        if path.suffix.lower() == ".dwg":
            print("  Convertendo DWG → DXF via ODA…", end=" ", flush=True)
            tmp_dxf = str(convert_dwg_to_dxf(str(path)))
            print("OK")
            dxf_path = tmp_dxf
        else:
            dxf_path = str(path)

        doc, _ = recover.readfile(dxf_path)
        msp = doc.modelspace()

        info = analyze_dxf(doc)
        dominant = find_dominant_text_layer(msp)
        positions = collect_text_positions(msp, layer=None)
        positions = _filter_meaningful(positions)

        qualidade, motivo = _detect_quality(info, positions)

        icon  = _QUALITY_ICONS[qualidade]
        label = _QUALITY_LABELS[qualidade]

        print(f"\n  Qualidade do DWG: {icon}  {label}")
        print(f"\n  Justificativa:\n    {motivo}")

        print(f"\n  Detalhes técnicos:")
        if info.extents:
            dx = info.extents[2] - info.extents[0]
            dy = info.extents[3] - info.extents[1]
            shorter, longer = min(dx, dy), max(dx, dy)
            aspect = longer / max(shorter, 0.001)
            print(f"    Dimensões:        {dx:.0f} × {dy:.0f} unidades")
            print(f"    Proporção:        {aspect:.1f}:1")
            print(f"    Dimensão menor:   {shorter:.0f} unidades")
        print(f"    Total entidades:  {info.n_entities}")
        print(f"    Textos totais:    {info.n_texts}")
        print(f"    Textos (filtrado):{len(positions)}")
        print(f"    Layer dominante:  {dominant or '—'}")
        print(f"    Versão DXF:       {info.version}")
        print(f"    Unidades:         {info.units_name} (código {info.units})")

    except Exception as exc:
        print(f"\n  ❌ Erro ao processar: {exc}")
    finally:
        if tmp_dxf:
            try:
                Path(tmp_dxf).unlink(missing_ok=True)
            except OSError:
                pass


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python analyze_quality.py arquivo1.dwg [arquivo2.dwg ...]")
        sys.exit(1)

    arquivos = [Path(a) for a in sys.argv[1:]]
    inexistentes = [a for a in arquivos if not a.exists()]
    if inexistentes:
        for a in inexistentes:
            print(f"Arquivo não encontrado: {a}")
        sys.exit(1)

    print(f"\nAnalisando {len(arquivos)} arquivo(s)…")
    for arq in arquivos:
        _analyze_file(arq)

    print(f"\n{'═'*72}\n")


if __name__ == "__main__":
    main()
