"""Almacenamiento de archivos con subida y descarga directas por SAS.

Arquitectura objetivo (§2.1 y §10 de la especificación): los archivos **no
pasan por la API**. La API emite una SAS URL de escritura, el navegador sube el
blob directo a Azure, y luego confirma con `POST /documentos/{id}/archivos`
enviando el `blob_path`. La descarga funciona igual con una SAS de lectura de
corta vida.

Backends (STORAGE_BACKEND):
  azure  Azure Blob Storage. Con `azure_storage_account` usa identidad
         administrada y SAS de usuario delegado (sin clave de cuenta en
         configuración); con `azure_blob_conn` firma con la clave de cuenta.
  local  Disco. Para que el contrato de la API sea idéntico en desarrollo, las
         "SAS" son URLs firmadas de endpoints locales de subida y descarga.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import settings


@dataclass
class SasUpload:
    upload_url: str
    blob_path: str
    expires_at: datetime
    headers: dict


@dataclass
class SasDownload:
    download_url: str
    expires_at: datetime


class Storage(ABC):
    @abstractmethod
    def upload_url(self, blob_path: str, content_type: str | None) -> SasUpload: ...

    @abstractmethod
    def download_url(self, blob_path: str, filename: str) -> SasDownload: ...

    @abstractmethod
    def exists(self, blob_path: str) -> bool: ...

    @abstractmethod
    def size(self, blob_path: str) -> int | None: ...

    @abstractmethod
    def save(self, blob_path: str, data: bytes) -> None: ...

    @abstractmethod
    def read(self, blob_path: str) -> bytes: ...

    @abstractmethod
    def delete(self, blob_path: str) -> None: ...


# ------------------------------------------------------------------- local
def _firma_local(blob_path: str, exp: int, modo: str) -> str:
    msg = f"{modo}:{blob_path}:{exp}".encode()
    return hmac.new(settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verificar_firma_local(blob_path: str, exp: int, modo: str, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_firma_local(blob_path, exp, modo), sig)


class LocalStorage(Storage):
    def __init__(self, base_dir: str):
        self.base = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _full(self, blob_path: str) -> str:
        full = os.path.normpath(os.path.join(self.base, blob_path))
        if not full.startswith(os.path.normpath(self.base)):
            raise ValueError("Ruta inválida")
        return full

    def _url(self, blob_path: str, modo: str, ttl_min: int,
             extra: str = "") -> tuple[str, datetime]:
        exp = int(time.time()) + ttl_min * 60
        sig = _firma_local(blob_path, exp, modo)
        base = settings.public_base_url.rstrip("/")
        ruta = "blobs/upload" if modo == "w" else "blobs/download"
        url = f"{base}/api/v1/{ruta}?blob_path={blob_path}&exp={exp}&sig={sig}{extra}"
        return url, datetime.fromtimestamp(exp, timezone.utc)

    def upload_url(self, blob_path: str, content_type: str | None) -> SasUpload:
        url, exp = self._url(blob_path, "w", settings.sas_upload_ttl_min)
        return SasUpload(
            upload_url=url, blob_path=blob_path, expires_at=exp,
            headers={"Content-Type": content_type or "application/octet-stream"})

    def download_url(self, blob_path: str, filename: str) -> SasDownload:
        url, exp = self._url(blob_path, "r", settings.sas_download_ttl_min,
                             extra=f"&filename={filename}")
        return SasDownload(download_url=url, expires_at=exp)

    def exists(self, blob_path: str) -> bool:
        return os.path.exists(self._full(blob_path))

    def size(self, blob_path: str) -> int | None:
        try:
            return os.path.getsize(self._full(blob_path))
        except OSError:
            return None

    def save(self, blob_path: str, data: bytes) -> None:
        full = self._full(blob_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def read(self, blob_path: str) -> bytes:
        with open(self._full(blob_path), "rb") as f:
            return f.read()

    def delete(self, blob_path: str) -> None:
        try:
            os.remove(self._full(blob_path))
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------- azure
class AzureBlobStorage(Storage):
    """SAS de usuario delegado con identidad administrada, o SAS de cuenta.

    La identidad del Container App necesita el rol
    `Storage Blob Data Contributor` sobre la cuenta para poder pedir la clave de
    delegación de usuario.
    """

    def __init__(self):
        from azure.storage.blob import BlobServiceClient

        self.container = settings.azure_blob_container
        self._delegado = False
        self._account_key = None
        if settings.azure_storage_account:
            from azure.identity import DefaultAzureCredential
            self._cred = DefaultAzureCredential()
            self.account = settings.azure_storage_account
            self.client = BlobServiceClient(
                f"https://{self.account}.blob.core.windows.net", credential=self._cred)
            self._delegado = True
        elif settings.azure_blob_conn:
            self.client = BlobServiceClient.from_connection_string(settings.azure_blob_conn)
            self.account = self.client.account_name
            self._account_key = self.client.credential.account_key
        else:
            raise RuntimeError(
                "STORAGE_BACKEND=azure requiere AZURE_STORAGE_ACCOUNT "
                "(identidad administrada) o AZURE_BLOB_CONN")
        try:
            self.client.create_container(self.container)
        except Exception:
            pass    # ya existe o no hay permiso de creación

    def _blob(self, blob_path: str):
        return self.client.get_blob_client(self.container, blob_path)

    def _sas(self, blob_path: str, permisos, ttl_min: int,
             **kwargs) -> tuple[str, datetime]:
        from azure.storage.blob import generate_blob_sas

        ahora = datetime.now(timezone.utc)
        expira = ahora + timedelta(minutes=ttl_min)
        comun = dict(account_name=self.account, container_name=self.container,
                     blob_name=blob_path, permission=permisos, expiry=expira,
                     start=ahora - timedelta(minutes=2), **kwargs)
        if self._delegado:
            udk = self.client.get_user_delegation_key(
                key_start_time=ahora - timedelta(minutes=2),
                key_expiry_time=expira + timedelta(minutes=5),
            )
            token = generate_blob_sas(user_delegation_key=udk, **comun)
        else:
            token = generate_blob_sas(account_key=self._account_key, **comun)
        url = (f"https://{self.account}.blob.core.windows.net/"
               f"{self.container}/{blob_path}?{token}")
        return url, expira

    def upload_url(self, blob_path: str, content_type: str | None) -> SasUpload:
        from azure.storage.blob import BlobSasPermissions
        url, exp = self._sas(blob_path, BlobSasPermissions(create=True, write=True),
                             settings.sas_upload_ttl_min)
        return SasUpload(
            upload_url=url, blob_path=blob_path, expires_at=exp,
            headers={"x-ms-blob-type": "BlockBlob",
                     "Content-Type": content_type or "application/octet-stream"},
        )

    def download_url(self, blob_path: str, filename: str) -> SasDownload:
        from azure.storage.blob import BlobSasPermissions
        url, exp = self._sas(
            blob_path, BlobSasPermissions(read=True), settings.sas_download_ttl_min,
            content_disposition=f'attachment; filename="{filename}"',
        )
        return SasDownload(download_url=url, expires_at=exp)

    def exists(self, blob_path: str) -> bool:
        try:
            return self._blob(blob_path).exists()
        except Exception:
            return False

    def size(self, blob_path: str) -> int | None:
        try:
            return self._blob(blob_path).get_blob_properties().size
        except Exception:
            return None

    def save(self, blob_path: str, data: bytes) -> None:
        self._blob(blob_path).upload_blob(data, overwrite=True)

    def read(self, blob_path: str) -> bytes:
        return self._blob(blob_path).download_blob().readall()

    def delete(self, blob_path: str) -> None:
        try:
            self._blob(blob_path).delete_blob()
        except Exception:
            pass


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = (AzureBlobStorage() if settings.storage_backend == "azure"
                    else LocalStorage(settings.storage_dir))
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None


def make_blob_path(company_id, dueno_tipo: str, dueno_id, documento_id,
                   filename: str) -> str:
    """{company_id}/{tipo}/{dueno}/{documento}/{uuid}_{filename} (§2 de la spec)."""
    safe = os.path.basename(filename).replace("\\", "_")
    return f"{company_id}/{dueno_tipo}/{dueno_id}/{documento_id}/{uuid.uuid4().hex[:8]}_{safe}"


def make_tmp_path(company_id, prefijo: str, filename: str) -> str:
    """Blobs temporales de extracción IA; se purgan a las 24 horas."""
    safe = os.path.basename(filename).replace("\\", "_")
    return f"tmp/{company_id}/{prefijo}_{uuid.uuid4().hex[:8]}_{safe}"
