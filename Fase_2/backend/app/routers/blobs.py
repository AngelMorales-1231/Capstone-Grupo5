"""Endpoints de blob firmado para el backend de almacenamiento `local`.

**Estos dos endpoints solo tienen sentido con `STORAGE_BACKEND=local`.** En Azure
el navegador habla DIRECTO con el blob usando la SAS que emite
`POST /documentos/{id}/upload-url`, y la API no ve pasar el archivo. Para que el
contrato del frontend sea idéntico en desarrollo, `LocalStorage` emite URLs
firmadas que apuntan aquí: mismo `PUT` con el cuerpo crudo, mismo `GET` de
descarga, misma caducidad. Cambiar `STORAGE_BACKEND` a `azure` deja estas rutas
sin uso y no obliga a tocar una línea del cliente.

**No requieren token, y es deliberado: la firma ES la autorización**, igual que
una SAS de Azure. El HMAC cubre `(modo, blob_path, exp)` con `JWT_SECRET`, así que
una URL solo sirve para el blob y el modo para los que se emitió y caduca por sí
sola (`SAS_UPLOAD_TTL_MIN` / `SAS_DOWNLOAD_TTL_MIN`). Exigir además el `Bearer`
rompería el modelo: en Azure el navegador tampoco manda el token al storage.

Consecuencia asumida: quien tenga la URL puede usarla hasta que expire. Es el
mismo compromiso que cualquier SAS y la razón de que los TTL sean de minutos y
las URLs de descarga no se deban cachear.
"""
from __future__ import annotations

import logging
import mimetypes
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from ..config import settings
from ..deps import err
from ..services.storage import get_storage, verificar_firma_local

logger = logging.getLogger("acredittia.blobs")

router = APIRouter(prefix="/blobs", tags=["blobs"])

MAX_BYTES = settings.max_upload_mb * 1024 * 1024

MSG_SOLO_LOCAL = ("Este endpoint solo opera con STORAGE_BACKEND=local; con Azure "
                  "el navegador sube y descarga directamente contra el blob")


def _verificar(blob_path: str, exp: int, sig: str, modo: str) -> str:
    """Comprueba la firma y devuelve la ruta. 403 si no cuadra o si caducó."""
    if settings.storage_backend != "local":
        # Con Azure la SAS la valida el propio storage; servir el blob desde aquí
        # duplicaría el control de acceso en dos sitios.
        raise err(400, "BACKEND_NO_LOCAL", MSG_SOLO_LOCAL)
    ruta = (blob_path or "").strip()
    if not ruta:
        raise err(400, "BLOB_PATH_REQUERIDO", "Falta blob_path")
    if not verificar_firma_local(ruta, exp, modo, sig or ""):
        # Mismo código para firma incorrecta y para URL caducada: distinguirlos
        # solo ayudaría a quien esté probando firmas.
        raise err(403, "FIRMA_INVALIDA",
                  "La URL firmada no es válida o ya expiró")
    return ruta


@router.put("/upload")
async def subir(request: Request,
                blob_path: str = Query(...),
                exp: int = Query(...),
                sig: str = Query(...)):
    """Recibe el cuerpo crudo del archivo y lo guarda en el storage local.

    Equivale al `PUT` contra una SAS de escritura de Azure: sin multipart, sin
    campos de formulario y sin token. El cuerpo entero es el contenido del blob.
    """
    ruta = _verificar(blob_path, exp, sig, "w")
    datos = await request.body()
    if not datos:
        raise err(400, "ARCHIVO_VACIO", "El cuerpo de la petición está vacío")
    if len(datos) > MAX_BYTES:
        raise err(400, "ARCHIVO_MUY_GRANDE",
                  f"Máximo {settings.max_upload_mb} MB")
    get_storage().save(ruta, datos)
    logger.info("blob subido %s (%s bytes)", ruta, len(datos))
    # 201 con el tamaño real: es lo que el cliente confirmará después en
    # POST /documentos/{id}/archivos, donde se vuelve a leer del storage.
    return Response(status_code=201,
                    headers={"x-blob-path": ruta,
                             "x-blob-size": str(len(datos))})


@router.get("/download")
def descargar(blob_path: str = Query(...),
              exp: int = Query(...),
              sig: str = Query(...),
              filename: str | None = Query(None)):
    """Devuelve el contenido del blob como descarga.

    El `Content-Disposition` reproduce el `content_disposition` que la SAS de
    Azure lleva embebido, de modo que el navegador guarde el archivo con su
    nombre original y no con el hash de la ruta.
    """
    ruta = _verificar(blob_path, exp, sig, "r")
    storage = get_storage()
    if not storage.exists(ruta):
        raise err(404, "NO_ENCONTRADO", "El archivo no existe")
    nombre = os.path.basename(filename or ruta)
    try:
        datos = storage.read(ruta)
    except OSError:
        logger.exception("no se pudo leer el blob %s", ruta)
        raise err(404, "NO_ENCONTRADO", "El archivo no existe")
    media = mimetypes.guess_type(nombre)[0] or "application/octet-stream"
    return Response(
        content=datos, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{nombre}"',
                 # Una URL firmada caduca: cachearla dejaría el archivo
                 # accesible desde el navegador después de expirar.
                 "Cache-Control": "no-store"},
    )
