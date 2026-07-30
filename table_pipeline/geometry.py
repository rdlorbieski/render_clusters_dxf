"""
geometry.py — extração geométrica e detecção de tabelas por grade conectada.

A ideia central:
  • Uma tabela é uma GRADE de linhas (bordas das células) com textos dentro.
  • Rasterizando linhas + textos num grid de ocupação e achando componentes
    conectados, cada tabela vira um "blob" separado.
  • Dilatação leve fecha os gaps internos (entre células) sem fundir tabelas
    distintas (separadas por gaps maiores).
  • O bbox do componente que contém uma keyword é a tabela inteira — as
    bordas caem no gap externo, nunca no meio de uma célula.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage


# ─────────────────────────────────────────────────────────────────────────────
# Extração de segmentos de linha e caixas de texto
# ─────────────────────────────────────────────────────────────────────────────


def collect_segments(msp) -> list[tuple[float, float, float, float]]:
    """Extrai todos os segmentos de reta do modelspace.

    Cobre LINE, LWPOLYLINE e POLYLINE (as entidades que formam grades de
    tabela). Retorna lista de ``(x1, y1, x2, y2)`` em unidades DXF.
    """
    segs: list[tuple[float, float, float, float]] = []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                s, d = e.dxf.start, e.dxf.end
                segs.append((s.x, s.y, d.x, d.y))
            elif t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
                closed = bool(e.closed)
                _poly_to_segments(pts, closed, segs)
            elif t == "POLYLINE":
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                closed = bool(e.is_closed)
                _poly_to_segments(pts, closed, segs)
        except Exception:
            continue
    return segs


def _poly_to_segments(pts, closed, out) -> None:
    """Quebra uma polilinha em segmentos consecutivos."""
    n = len(pts)
    if n < 2:
        return
    for i in range(n - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        out.append((x1, y1, x2, y2))
    if closed:
        x1, y1 = pts[-1]
        x2, y2 = pts[0]
        out.append((x1, y1, x2, y2))


@dataclass
class TextBox:
    """Caixa estimada de uma entidade de texto."""
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    text: str
    height: float = 1.0   # altura da fonte (unidades DXF) — escala do texto

    @property
    def cx(self) -> float:
        return (self.x_min + self.x_max) / 2

    @property
    def cy(self) -> float:
        return (self.y_min + self.y_max) / 2


def collect_text_boxes(msp, char_w_factor: float = 0.62) -> list[TextBox]:
    """Extrai caixas estimadas de TEXT/MTEXT.

    A largura é estimada por ``n_chars × altura × char_w_factor`` (DXF não
    armazena a largura renderizada do texto). Suficiente para marcar
    ocupação no grid.
    """
    boxes: list[TextBox] = []
    for e in msp:
        t = e.dxftype()
        if t not in ("TEXT", "MTEXT"):
            continue
        try:
            ins = e.dxf.insert
            if t == "TEXT":
                raw = e.dxf.text or ""
                h = float(e.dxf.get("height", 0)) or 1.0
            else:
                raw = getattr(e.dxf, "text", "") or ""
                h = float(e.dxf.get("char_height", 0)) or 1.0
            if not raw.strip():
                continue
            # Estima a maior linha (MTEXT pode ter quebras \P)
            lines = raw.replace("\\P", "\n").split("\n")
            max_chars = max((len(ln) for ln in lines), default=len(raw))
            n_lines = max(len(lines), 1)
            w = max_chars * h * char_w_factor
            ht = n_lines * h * 1.4
            boxes.append(TextBox(ins.x, ins.y, ins.x + w, ins.y + ht, raw, height=h))
        except Exception:
            continue
    return boxes


# ─────────────────────────────────────────────────────────────────────────────
# Grid de ocupação + componentes conectados
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class OccupancyGrid:
    """Grid booleano de ocupação geométrica de uma região retangular."""
    grid: np.ndarray            # shape (H, W), True = ocupado
    region: tuple[float, float, float, float]  # (x0, y0, x1, y1)
    cell: float                 # tamanho da célula em unidades DXF

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """Converte coordenada do mundo → (row, col). Sem checar limites."""
        col = int((x - self.region[0]) / self.cell)
        row = int((y - self.region[1]) / self.cell)
        return row, col

    @property
    def shape(self) -> tuple[int, int]:
        return self.grid.shape


def build_occupancy(
    segments: list[tuple[float, float, float, float]],
    text_boxes: list[TextBox],
    region: tuple[float, float, float, float],
    cell: float,
    max_cells: int = 2200,
) -> OccupancyGrid:
    """Rasteriza segmentos + caixas de texto num grid booleano.

    Args:
        segments: lista de ``(x1,y1,x2,y2)``.
        text_boxes: caixas de texto.
        region: janela ``(x0,y0,x1,y1)`` a rasterizar.
        cell: tamanho desejado da célula (clampado p/ não estourar max_cells).
        max_cells: lado máximo do grid (proteção de memória).
    """
    x0, y0, x1, y1 = region
    w_world = max(x1 - x0, 1e-6)
    h_world = max(y1 - y0, 1e-6)

    # Garante que o grid não exceda max_cells por lado
    cell = max(cell, w_world / max_cells, h_world / max_cells)
    W = int(w_world / cell) + 1
    H = int(h_world / cell) + 1
    grid = np.zeros((H, W), dtype=bool)

    # Rasteriza segmentos (amostragem ao longo da reta)
    for (sx1, sy1, sx2, sy2) in segments:
        # ignora o que está totalmente fora da região (bbox rápido)
        if (max(sx1, sx2) < x0 or min(sx1, sx2) > x1 or
                max(sy1, sy2) < y0 or min(sy1, sy2) > y1):
            continue
        length = ((sx2 - sx1) ** 2 + (sy2 - sy1) ** 2) ** 0.5
        steps = int(length / cell) + 1
        for i in range(steps + 1):
            t = i / steps if steps else 0.0
            x = sx1 + (sx2 - sx1) * t
            y = sy1 + (sy2 - sy1) * t
            c = int((x - x0) / cell)
            r = int((y - y0) / cell)
            if 0 <= r < H and 0 <= c < W:
                grid[r, c] = True

    # Marca caixas de texto (retângulo preenchido)
    for tb in text_boxes:
        if tb.x_max < x0 or tb.x_min > x1 or tb.y_max < y0 or tb.y_min > y1:
            continue
        c0 = max(int((tb.x_min - x0) / cell), 0)
        c1 = min(int((tb.x_max - x0) / cell), W - 1)
        r0 = max(int((tb.y_min - y0) / cell), 0)
        r1 = min(int((tb.y_max - y0) / cell), H - 1)
        if c1 >= c0 and r1 >= r0:
            grid[r0:r1 + 1, c0:c1 + 1] = True

    return OccupancyGrid(grid=grid, region=region, cell=cell)


@dataclass
class Component:
    """Componente conectado = candidato a tabela/bloco."""
    bbox: tuple[float, float, float, float]   # (xmin,ymin,xmax,ymax) no mundo
    label: int
    mask_area_cells: int


def find_components(occ: OccupancyGrid, gap_cells: int = 2) -> np.ndarray:
    """Dilata o grid e rotula componentes conectados.

    A dilatação (gap_cells) fecha os vãos internos da tabela (entre células)
    para que a grade vire um único componente. Gaps maiores que ``gap_cells``
    (espaço entre tabelas distintas) permanecem separados.

    Returns:
        labels: ndarray (H,W) com o id do componente de cada célula (0 = vazio).
    """
    g = occ.grid
    if gap_cells > 0:
        g = ndimage.binary_dilation(g, iterations=gap_cells)
    # conectividade 8 (inclui diagonais) — grades têm cantos
    structure = np.ones((3, 3), dtype=int)
    labels, _ = ndimage.label(g, structure=structure)
    return labels


def component_bbox(
    occ: OccupancyGrid,
    labels: np.ndarray,
    label_id: int,
    use_original: bool = True,
) -> tuple[float, float, float, float] | None:
    """Bbox no mundo das células do componente ``label_id``.

    Se ``use_original`` (padrão), restringe à ocupação ORIGINAL (não dilatada)
    dentro do componente — bordas justas, coincidindo com as linhas externas
    da tabela.
    """
    mask = labels == label_id
    if use_original:
        mask = mask & occ.grid
    rows, cols = np.where(mask)
    if rows.size == 0:
        return None
    x0, y0, _ = occ.region[0], occ.region[1], occ.cell
    cell = occ.cell
    xmin = x0 + cols.min() * cell
    xmax = x0 + (cols.max() + 1) * cell
    ymin = y0 + rows.min() * cell
    ymax = y0 + (rows.max() + 1) * cell
    return (xmin, ymin, xmax, ymax)
