"""
Conversão DWG → DXF usando o ODA File Converter.

Ordem de busca do binário:
  Windows:
    1. bin/ODAFileConverter.exe         (cópia manual no projeto)
    2. C:/Program Files/ODA/ODAFileConverter*/ODAFileConverter.exe  (instalação via .msi)
  Linux/Docker:
    1. /usr/bin/ODAFileConverter        (instalado via dpkg no container)
    2. bin/ODAFileConverter             (cópia manual, fallback)
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent


def _oda_binary() -> Path:
    if platform.system() == "Windows":
        local = _BASE_DIR / "bin" / "ODAFileConverter.exe"
        if local.exists():
            return local

        candidates = sorted(
            Path("C:/Program Files/ODA").glob("ODAFileConverter*/ODAFileConverter.exe"),
            reverse=True,
        )
        if candidates:
            return candidates[0]

        return local  # não existe — erro claro em convert_dwg_to_dxf

    # Linux / Docker: instalado via dpkg em /usr/bin/
    system_bin = Path("/usr/bin/ODAFileConverter")
    if system_bin.exists():
        return system_bin

    return _BASE_DIR / "bin" / "ODAFileConverter"  # fallback manual


def convert_dwg_to_dxf(dwg_path: str) -> Path:
    """
    Converte um arquivo DWG em DXF via ODA File Converter.

    Retorna o Path do arquivo .dxf gerado em um diretório temporário.
    O chamador é responsável por deletar o arquivo após o uso.

    Raises:
        FileNotFoundError: se o DWG ou o binário ODA não forem encontrados.
        RuntimeError: se o conversor falhar ou não gerar saída.
        subprocess.TimeoutExpired: se a conversão demorar mais de 120 s.
    """
    src = Path(dwg_path)
    if not src.exists():
        raise FileNotFoundError(f"Arquivo DWG não encontrado: {src}")

    oda = _oda_binary()
    if not oda.exists():
        raise FileNotFoundError(
            f"ODA File Converter não encontrado em {oda}. "
            "Coloque o binário na pasta bin/ do projeto."
        )

    # ODA opera sobre pastas, não arquivos individuais.
    in_dir  = Path(tempfile.mkdtemp(prefix="oda_in_"))
    out_dir = Path(tempfile.mkdtemp(prefix="oda_out_"))
    try:
        shutil.copy2(src, in_dir / src.name)

        # Sintaxe: ODAFileConverter <in> <out> <version> <type> <recurse> <audit>
        oda_cmd = [
            str(oda),
            str(in_dir),
            str(out_dir),
            "ACAD2018",  # versão do DXF de saída
            "DXF",       # formato de saída
            "0",         # sem recursão em subpastas
            "1",         # auditoria/recuperação automática
        ]
        # No Linux usa xvfb-run para evitar erros de display em ambiente headless
        if platform.system() != "Windows":
            cmd = ["xvfb-run", "-a"] + oda_cmd
        else:
            cmd = oda_cmd

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"ODA File Converter encerrou com código {proc.returncode}. "
                f"stderr: {proc.stderr.strip() or '(vazio)'} "
                f"stdout: {proc.stdout.strip() or '(vazio)'}"
            )

        dxf_files = list(out_dir.glob("*.dxf"))
        if not dxf_files:
            raise RuntimeError(
                "ODA File Converter não gerou nenhum arquivo DXF. "
                f"stdout: {proc.stdout.strip()} stderr: {proc.stderr.strip()}"
            )

        # Move para fora dos diretórios temporários antes de limpá-los.
        dxf_dest = Path(tempfile.mktemp(suffix=".dxf", prefix="render_dxf_"))
        shutil.move(str(dxf_files[0]), dxf_dest)
        return dxf_dest

    finally:
        shutil.rmtree(in_dir,  ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
