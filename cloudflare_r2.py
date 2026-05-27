import os
import uuid
from pathlib import Path

import boto3
from botocore.client import Config
from dotenv import load_dotenv

load_dotenv()

BUCKET = os.environ.get("R2_BUCKET", "trem")
BASE_URL = os.environ.get("R2_BASE_URL", "https://pub-c5f4dc039c0d4192a68829eab386854a.r2.dev")

PNG_PREFIX = "dxf_images/"


def _r2_client():
    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key, secret_key]):
        raise RuntimeError(
            "Credenciais R2 não configuradas. "
            "Defina R2_ACCOUNT_ID, R2_ACCESS_KEY_ID e R2_SECRET_ACCESS_KEY."
        )

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def download_dxf_from_r2(key: str) -> tuple[bytes, str]:
    """Baixa um DXF do R2. key pode ser a chave completa ou só o nome do arquivo."""
    if "/" not in key:
        key = f"{DXF_PREFIX}{key}"
    client = _r2_client()
    try:
        obj = client.get_object(Bucket=BUCKET, Key=key)
    except Exception as e:
        raise RuntimeError(f"Erro ao baixar DXF do R2 (key={key!r}): {e}") from e
    return obj["Body"].read(), key


def upload_png_to_r2(png_path: str | Path) -> str:
    """Faz upload de um PNG para o R2 e retorna a key do objeto."""
    key = f"{PNG_PREFIX}{uuid.uuid4().hex}.png"
    client = _r2_client()
    try:
        with open(png_path, "rb") as f:
            client.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=f.read(),
                ContentType="image/png",
            )
    except Exception as e:
        raise RuntimeError(f"Erro ao enviar PNG para o R2: {e}") from e
    return key


def download_png_from_r2(key: str) -> bytes:
    """Baixa um PNG do R2 pela key e retorna os bytes."""
    if "/" not in key:
        key = f"{PNG_PREFIX}{key}"
    client = _r2_client()
    try:
        obj = client.get_object(Bucket=BUCKET, Key=key)
    except client.exceptions.NoSuchKey:
        raise FileNotFoundError(f"Arquivo '{key}' não encontrado no bucket R2.")
    except Exception as e:
        raise RuntimeError(f"Erro ao baixar PNG do R2: {e}") from e
    return obj["Body"].read()
