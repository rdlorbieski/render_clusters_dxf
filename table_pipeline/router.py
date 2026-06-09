"""
router.py — endpoints FastAPI do pipeline de tabelas.

  POST /tables/extract  → ZIP com as tabelas em alta resolução (legível por LLM)
  POST /tables/plot     → PNG do desenho com os retângulos das tabelas (debug)

São incluídos no app principal via `app.include_router(table_pipeline.router)`.
"""
from __future__ import annotations

import io
import tempfile
import zipfile
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, HTTPException
from fastapi.responses import JSONResponse, Response
from ezdxf import recover

from .pipeline import run_pipeline, render_tables

router = APIRouter(tags=["tables"])


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
    Pipeline completo:
      1. Avalia qualidade — se baixa, retorna 200 com o motivo (não insiste).
      2-4. Detecta tabelas (grade conectada) e pontua por keywords PSCIP.
      5. Loga tudo no console do uvicorn.
      6. Renderiza as tabelas de maior score em alta resolução → ZIP.
    """
    from api import _load_dxf_to_tmp, _cleanup

    dxf_path = _load_dxf_to_tmp(file)
    try:
        doc, _ = recover.readfile(dxf_path)

        result = run_pipeline(
            doc, n=n, cell_factor=cell_factor, gap_factor=gap_factor,
            roi_margin_factor=roi_margin_factor, group_factor=group_factor)

        # Passo 1 — qualidade baixa: devolve o motivo, não insiste
        if result.aborted:
            return JSONResponse(status_code=200, content={
                "status": "qualidade_baixa",
                "qualidade_dwg": result.qualidade,
                "motivo": result.motivo,
                "tabelas": [],
            })

        if not result.tables:
            return JSONResponse(status_code=200, content={
                "status": "nenhuma_tabela",
                "qualidade_dwg": result.qualidade,
                "motivo": "Nenhuma tabela com keywords PSCIP foi localizada.",
                "tabelas": [],
            })

        # Passo 6 — render (lógica compartilhada com o script batch)
        rendered = render_tables(doc, result)

        if not rendered:
            return JSONResponse(status_code=200, content={
                "status": "nenhuma_tabela_renderizavel",
                "qualidade_dwg": result.qualidade,
                "motivo": "Tabelas detectadas, mas nenhuma pôde ser renderizada.",
                "tabelas": [],
            })

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

        # Adiciona o manifest JSON dentro do ZIP
        import json
        with zipfile.ZipFile(zip_buf, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps({
                "qualidade_dwg": result.qualidade,
                "motivo": result.motivo,
                "text_height_global": round(result.text_height, 2),
                "tabelas": manifest,   # cada tabela traz seu próprio text_height e dpi
            }, ensure_ascii=False, indent=2))

        zip_buf.seek(0)
        return Response(
            content=zip_buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="tabelas.zip"',
                "X-Qualidade-DWG": result.qualidade,
                "X-Tabelas": str(len(manifest)),
            },
        )
    finally:
        _cleanup(dxf_path)


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

        if result.aborted:
            return JSONResponse(status_code=200, content={
                "status": "qualidade_baixa",
                "qualidade_dwg": result.qualidade,
                "motivo": result.motivo,
            })
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
    finally:
        _cleanup(dxf_path)
        if png_path:
            _cleanup(png_path)
