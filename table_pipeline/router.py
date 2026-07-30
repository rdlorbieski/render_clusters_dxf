"""
router.py — endpoints FastAPI do pipeline de tabelas.

  POST /tables/extract → ZIP com as tabelas em alta resolução (legível por LLM)
  POST /tables/info    → JSON: qualidade, parâmetros e lista de tabelas
  POST /tables/plot    → PNG do desenho com os retângulos (debug visual)

Incluídos no app principal via app.include_router(table_pipeline.router).
"""
from __future__ import annotations

import io
import json
import tempfile
import zipfile
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, HTTPException
from fastapi.responses import JSONResponse, Response
from ezdxf import recover

from .pipeline import run_pipeline, render_tables, PipelineResult
from .exceptions import (
    LowQualityDXFError, NoTablesDetectedError, NoRenderableTablesError,
)

router = APIRouter(tags=["tables"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de resposta — traduzem o domínio em JSON
# ─────────────────────────────────────────────────────────────────────────────


def _resposta_informativa(status: str, qualidade: str, motivo: str) -> JSONResponse:
    """200 informativo quando não há ZIP a entregar (baixa qualidade / sem tabela)."""
    return JSONResponse(status_code=200, content={
        "status": status,
        "qualidade_dwg": qualidade,
        "motivo": motivo,
        "tabelas": [],
    })


def _tabela_para_json(index: int, t) -> dict:
    """Serializa uma Table para o JSON do /tables/info."""
    x0, y0, x1, y1 = t.bbox
    return {
        "index": index,
        "score": round(t.score, 1),
        "keyword_count": t.keyword_count,
        "text_count": t.text_count,
        "text_height": round(t.text_height, 3),
        "bbox": {
            "x_min": round(x0, 2), "y_min": round(y0, 2),
            "x_max": round(x1, 2), "y_max": round(y1, 2),
            "width": round(x1 - x0, 2), "height": round(y1 - y0, 2),
        },
        "keywords": t.keywords,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint — /tables/extract  (PASSOS 1→6: detecção + render + ZIP)
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/tables/extract",
    summary="Detecta tabelas com keywords PSCIP e exporta em alta resolução (ZIP)",
)
async def extract_tables_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    n: Annotated[int, Query(ge=1, le=20, description="Máx. de tabelas")] = 5,
    cell_factor: Annotated[
        float, Query(gt=0, description="Célula do grid = altura_texto × isto")] = 1.0,
    gap_factor: Annotated[
        float, Query(gt=0, description="Dilatação p/ fechar vãos internos")] = 2.5,
    roi_margin_factor: Annotated[
        float, Query(gt=0, description="Margem de busca ao redor das keywords")] = 60.0,
    group_factor: Annotated[
        float, Query(gt=0, description="Raio p/ agrupar keywords vizinhas")] = 25.0,
):
    """
    PASSO 1–4  detecção das tabelas        (run_pipeline)
    PASSO 5    render em alta resolução     (render_tables)
    PASSO 6    empacota PNGs + manifest     (ZIP)

    Qualidade baixa / sem tabela → 200 informativo (não insiste).
    """
    from api import _load_dxf_to_tmp, _cleanup

    dxf_path = _load_dxf_to_tmp(file)
    try:
        doc, _ = recover.readfile(dxf_path)

        result = run_pipeline(                       # PASSOS 1–4
            doc, n=n, cell_factor=cell_factor, gap_factor=gap_factor,
            roi_margin_factor=roi_margin_factor, group_factor=group_factor)
        rendered = _renderizar_ou_falhar(doc, result)   # PASSO 5
        return _empacotar_zip(result, rendered)         # PASSO 6

    except LowQualityDXFError as e:
        return _resposta_informativa("qualidade_baixa", e.qualidade, e.motivo)
    except NoTablesDetectedError as e:
        return _resposta_informativa("nenhuma_tabela", e.qualidade, e.motivo)
    except NoRenderableTablesError as e:
        return _resposta_informativa("nenhuma_tabela_renderizavel", e.qualidade, e.motivo)
    finally:
        _cleanup(dxf_path)


def _renderizar_ou_falhar(doc, result: PipelineResult) -> list[dict]:
    """PASSO 5 — render; converte resultado vazio em exceção de domínio."""
    if not result.tables:
        raise NoTablesDetectedError(result.qualidade)
    rendered = render_tables(doc, result)
    if not rendered:
        raise NoRenderableTablesError(result.qualidade)
    return rendered


def _empacotar_zip(result: PipelineResult, rendered: list[dict]) -> Response:
    """PASSO 6 — monta o ZIP com os PNGs + manifest.json."""
    zip_buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rendered:
            zf.writestr(r["name"], r["png"])
            x0, y0, x1, y1 = r["bbox"]
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
        zf.writestr("manifest.json", json.dumps({
            "qualidade_dwg": result.qualidade,
            "motivo": result.motivo,
            "text_height_global": round(result.text_height, 2),
            "tabelas": manifest,
        }, ensure_ascii=False, indent=2))

    zip_buf.seek(0)
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="tabelas.zip"',
            "X-Qualidade-DWG": result.qualidade,
            "X-Motivo": result.motivo.encode("ascii", errors="replace").decode("ascii"),
            "X-Gap-Cells": str(result.gap_cells),
            "X-Tabelas": str(len(manifest)),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint — /tables/info  (apenas JSON, sem render)
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/tables/info",
    summary="Detecta tabelas PSCIP e retorna JSON com qualidade, parâmetros e lista de tabelas",
)
async def info_tables_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    n: Annotated[int, Query(ge=1, le=20, description="Máx. de tabelas")] = 5,
    cell_factor: Annotated[float, Query(gt=0)] = 1.0,
    gap_factor: Annotated[float, Query(gt=0)] = 2.5,
    roi_margin_factor: Annotated[float, Query(gt=0)] = 60.0,
    group_factor: Annotated[float, Query(gt=0)] = 25.0,
):
    """
    Roda os PASSOS 1–4 e devolve apenas JSON — sem renderizar nem montar ZIP.
    Ideal para inspecionar quais tabelas sairiam antes de chamar /tables/extract.
    """
    from api import _load_dxf_to_tmp, _cleanup

    dxf_path = _load_dxf_to_tmp(file)
    try:
        doc, _ = recover.readfile(dxf_path)
        result = run_pipeline(
            doc, n=n, cell_factor=cell_factor, gap_factor=gap_factor,
            roi_margin_factor=roi_margin_factor, group_factor=group_factor)

        return JSONResponse({
            "qualidade_dwg": result.qualidade,
            "motivo": result.motivo,
            "aborted": False,
            "text_height": round(result.text_height, 3),
            "cell": round(result.cell, 3),
            "gap_cells": result.gap_cells,
            "total_tabelas": len(result.tables),
            "tabelas": [_tabela_para_json(i, t)
                        for i, t in enumerate(result.tables, 1)],
        })

    except LowQualityDXFError as e:
        # Qualidade baixa: reporta o motivo (sem parâmetros de grid).
        return JSONResponse({
            "qualidade_dwg": e.qualidade,
            "motivo": e.motivo,
            "aborted": True,
            "text_height": 0.0,
            "cell": 0.0,
            "gap_cells": 0,
            "total_tabelas": 0,
            "tabelas": [],
        })
    finally:
        _cleanup(dxf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint — /tables/plot  (PNG de debug com os retângulos sobrepostos)
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/tables/plot",
    summary="Plota os retângulos das tabelas sobre o desenho (debug visual)",
)
async def plot_tables_endpoint(
    file: Annotated[UploadFile, File(description="Arquivo DXF ou DWG")],
    n: Annotated[int, Query(ge=1, le=20)] = 5,
    full: Annotated[bool, Query(description="True = prancha inteira")] = False,
    cell_factor: Annotated[float, Query(gt=0)] = 1.0,
    gap_factor: Annotated[float, Query(gt=0)] = 2.5,
    roi_margin_factor: Annotated[float, Query(gt=0)] = 60.0,
    group_factor: Annotated[float, Query(gt=0)] = 25.0,
):
    """Mesma detecção do /tables/extract, mas devolve um PNG com os
    retângulos sobrepostos — para validar visualmente os recortes."""
    from api import _load_dxf_to_tmp, _cleanup
    from dxf_render import (
        analyze_dxf, auto_detect_color_policy, build_config,
        suggest_lineweight, render_overview_with_rects,
    )

    dxf_path = _load_dxf_to_tmp(file)
    png_path: str | None = None
    try:
        doc, _ = recover.readfile(dxf_path)
        result = run_pipeline(
            doc, n=n, cell_factor=cell_factor, gap_factor=gap_factor,
            roi_margin_factor=roi_margin_factor, group_factor=group_factor)

        if not result.tables:
            raise HTTPException(status_code=404,
                                detail="Nenhuma tabela com keywords detectada.")

        info = analyze_dxf(doc)
        color_policy = auto_detect_color_policy(info)
        config = build_config(color_policy=color_policy,
                              min_lineweight=suggest_lineweight(1000, 200))

        rects = [t.bbox for t in result.tables]
        labels = [f"{i} (kw={t.keyword_count}, sc={t.score:.0f})"
                  for i, t in enumerate(result.tables, 1)]
        region = info.extents if (full and info.extents) else None

        png_path = tempfile.mktemp(suffix=".png")
        ok = render_overview_with_rects(
            doc, rects, png_path, labels=labels, region=region,
            config=config, verbose=False)
        if not ok:
            raise HTTPException(status_code=404, detail="Região vazia.")

        with open(png_path, "rb") as f:
            png = f.read()
        return Response(
            content=png, media_type="image/png",
            headers={
                "Content-Disposition": 'attachment; filename="tables_plot.png"',
                "X-Qualidade-DWG": result.qualidade,
                "X-Tabelas": str(len(rects)),
            },
        )

    except LowQualityDXFError as e:
        return JSONResponse(status_code=200, content={
            "status": "qualidade_baixa",
            "qualidade_dwg": e.qualidade,
            "motivo": e.motivo,
        })
    finally:
        _cleanup(dxf_path)
        if png_path:
            _cleanup(png_path)
