"""
extract_tables_batch.py
═════════════════════════════════════════════════════════════════════════════
Roda o MESMO pipeline do endpoint /tables/extract localmente, sem servidor.

Para cada arquivo listado em ARQUIVOS:
  • converte DWG → DXF (se necessário)
  • detecta tabelas com keywords PSCIP
  • renderiza cada tabela em alta resolução (DPI dinâmico, igual ao endpoint)
  • salva os PNGs + manifest.json em  output/<nome_do_arquivo>/

Uso:
  1. Preencha o array ARQUIVOS abaixo com caminhos ABSOLUTOS de .dwg/.dxf
  2. python batch_extract/extract_tables_batch.py
"""
from __future__ import annotations

# ── backend headless ANTES de qualquer import que puxe matplotlib/pyplot ──────
import matplotlib
matplotlib.use("Agg")

import io
import json
import sys
from pathlib import Path

# Garante que a raiz do projeto está no sys.path (para importar api/dxf_render/…)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import logging
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from ezdxf import recover

from converter import convert_dwg_to_dxf
from table_pipeline.pipeline import run_pipeline, render_tables

# UTF-8 no stdout (acentos no console do Windows)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# 1) EDITE AQUI — caminhos absolutos dos DWG/DXF a processar
# ─────────────────────────────────────────────────────────────────────────────
ARQUIVOS: list[str] = [
    r"C:\Users\Usuario\Downloads\Compactados\Projetos aprovados-20260427T203500Z-3-001\Projetos aprovados\PRJ2026002618\PRJ2026002618.dxf", # qualidade baixa
    r"C:\Users\Usuario\Downloads\Compactados\Projetos aprovados-20260427T203500Z-3-001\Projetos aprovados\PRJ2026004905\PRJ2026004905.dxf", # qualidade baixa
    r"C:\Users\Usuario\Downloads\Compactados\Projetos aprovados-20260427T203500Z-3-001\Projetos aprovados\PRJ2026005112\PRJ2026005112.dxf",
    r"C:\Users\Usuario\Downloads\Compactados\Projetos aprovados-20260427T203500Z-3-001\Projetos aprovados\PRJ2026006960\PRJ2026006960.dxf",
    r"C:\Users\Usuario\Downloads\Pacote de Projetos PPCI\Pacote de Projetos PPCI\Comercio\PSCIP_VEGETAL AGRONEGOCIOS_FORMOSA-GOIAS_R00 - 889.98M2.dxf",
    r"C:\Users\Usuario\Downloads\Pacote de Projetos PPCI\Pacote de Projetos PPCI\Comercio\PSCIP_ALTO NORTE SEMENTES_FORMOSA-GOIAS_R01 - 1.441.44M2.dwg",
    r"C:\Users\Usuario\Downloads\Pacote de Projetos PPCI\Pacote de Projetos PPCI\Igreja\PJ_PPCI_IGREJA PRESBITERIANA BETANIA-GYN_R01 - 2.771.95M2.dwg",
]

# REPEAT = False → se a pasta de saída do arquivo já tiver imagens, PULA.
# REPEAT = True  → reprocessa tudo, mesmo que já existam imagens.
REPEAT = False

# Parâmetros (mesmos defaults do endpoint /tables/extract)
N_TABELAS = 5
CELL_FACTOR = 1.0
GAP_FACTOR = 2.5
ROI_MARGIN_FACTOR = 60.0
GROUP_FACTOR = 25.0

# Pasta raiz de saída (dentro desta pasta batch_extract/)
OUTPUT_ROOT = Path(__file__).resolve().parent / "output"


# ─────────────────────────────────────────────────────────────────────────────
# Processamento
# ─────────────────────────────────────────────────────────────────────────────


def _already_processed(outdir: Path) -> bool:
    """True se a pasta já contém resultado (PNGs ou marcador de qualidade baixa)."""
    if not outdir.is_dir():
        return False
    has_png = any(outdir.glob("*.png"))
    has_low = (outdir / "_qualidade_baixa.txt").exists()
    return has_png or has_low


def _clean_outputs(outdir: Path) -> None:
    """Remove resultados de uma execução anterior (PNGs, manifest, marcador)."""
    for p in list(outdir.glob("*.png")) + [
        outdir / "manifest.json", outdir / "_qualidade_baixa.txt"
    ]:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _process(path: Path) -> None:
    stem = path.stem
    outdir = OUTPUT_ROOT / stem

    print(f"\n{'═' * 72}")
    print(f"  {path.name}")
    print(f"{'═' * 72}")

    # REPEAT=False → pula se já houver resultado nesta pasta
    if not REPEAT and _already_processed(outdir):
        print("  ⏭️  Já processado (pasta contém arquivos) — pulando. "
              "Use REPEAT = True para reprocessar.")
        return

    outdir.mkdir(parents=True, exist_ok=True)
    # REPEAT=True → limpa resultados antigos (nomes mudam com o score)
    _clean_outputs(outdir)

    tmp_dxf: str | None = None
    try:
        # Converte DWG → DXF se necessário
        if path.suffix.lower() == ".dwg":
            print("  Convertendo DWG → DXF…", end=" ", flush=True)
            tmp_dxf = str(convert_dwg_to_dxf(str(path)))
            dxf_path = tmp_dxf
            print("OK")
        else:
            dxf_path = str(path)

        doc, _ = recover.readfile(dxf_path)

        # Passos 1–5 (qualidade, detecção, score, log no console)
        result = run_pipeline(
            doc, n=N_TABELAS, cell_factor=CELL_FACTOR, gap_factor=GAP_FACTOR,
            roi_margin_factor=ROI_MARGIN_FACTOR, group_factor=GROUP_FACTOR)

        if result.aborted:
            print(f"  ⚠️  Qualidade BAIXA — pulando. Motivo:\n     {result.motivo}")
            (outdir / "_qualidade_baixa.txt").write_text(
                result.motivo, encoding="utf-8")
            return

        if not result.tables:
            print("  Nenhuma tabela com keywords PSCIP localizada.")
            return

        # Passo 6 — render em alta resolução (lógica idêntica ao endpoint)
        rendered = render_tables(doc, result)
        if not rendered:
            print("  Tabelas detectadas, mas nenhuma pôde ser renderizada.")
            return

        manifest = []
        for r in rendered:
            (outdir / r["name"]).write_bytes(r["png"])
            x0, y0, x1, y1 = r["bbox"]
            kb = len(r["png"]) / 1024
            print(f"    → {r['name']}  ({kb:.0f} KB, dpi={r['dpi']})")
            manifest.append({
                "file": r["name"],
                "score": round(r["score"], 1),
                "keyword_count": r["keyword_count"],
                "text_count": r["text_count"],
                "text_height": round(r["text_height"], 3),
                "dpi": r["dpi"],
                "bbox": {"x_min": round(x0, 2), "y_min": round(y0, 2),
                         "x_max": round(x1, 2), "y_max": round(y1, 2)},
                "keywords": r["keywords"],
            })

        (outdir / "manifest.json").write_text(json.dumps({
            "arquivo": path.name,
            "qualidade_dwg": result.qualidade,
            "motivo": result.motivo,
            "text_height_global": round(result.text_height, 2),
            "tabelas": manifest,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"  ✅ {len(manifest)} tabela(s) salva(s) em: {outdir}")

    except Exception as exc:
        import traceback
        print(f"  ❌ ERRO: {exc}")
        traceback.print_exc()
    finally:
        # Limpa o DXF temporário gerado a partir do DWG
        if tmp_dxf:
            try:
                Path(tmp_dxf).unlink(missing_ok=True)
            except OSError:
                pass


def main() -> None:
    # Permite também passar caminhos pela linha de comando (sobrepõe ARQUIVOS)
    arquivos = [Path(a) for a in sys.argv[1:]] or [Path(a) for a in ARQUIVOS]

    if not arquivos:
        print("Nenhum arquivo. Edite ARQUIVOS ou passe caminhos como argumento.")
        sys.exit(1)

    faltando = [a for a in arquivos if not a.exists()]
    if faltando:
        print("Arquivos não encontrados:")
        for a in faltando:
            print(f"  - {a}")
        sys.exit(1)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Saída em: {OUTPUT_ROOT.resolve()}")

    for a in arquivos:
        _process(a)

    print(f"\n{'═' * 72}")
    print(f"Concluído. Imagens em: {OUTPUT_ROOT.resolve()}")
    print(f"{'═' * 72}\n")


if __name__ == "__main__":
    main()
