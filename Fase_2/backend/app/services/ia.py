"""Revisión IA de documentos: interfaz + implementación simulada y adaptador Claude.

La simulada replica el comportamiento del wireframe (hallazgos aleatorios pero
deterministas por archivo). El adaptador Claude queda listo: se activa con
IA_BACKEND=claude y ANTHROPIC_API_KEY.
"""
import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Documento, DocumentoArchivo, IaHallazgo, IaReview


@dataclass
class Hallazgo:
    tipo: str      # error | warning | info
    codigo: str
    mensaje: str


@dataclass
class ResultadoIA:
    resultado: str                 # validado | con_observaciones | con_errores
    confianza: float
    campos_extraidos: dict = field(default_factory=dict)
    hallazgos: list[Hallazgo] = field(default_factory=list)


ERRORES = [
    ("DOC_ILEGIBLE", "Documento ilegible: resolución inferior a 150 DPI."),
    ("FIRMA_NO_ENCONTRADA", "No se encontró firma en el documento."),
    ("VENCIMIENTO_AUSENTE", "No se detectó fecha de vencimiento."),
    ("RUT_NO_COINCIDE", "El RUT del documento no coincide con el del sujeto."),
    ("SELLO_NO_RECONOCIDO", "Sello o timbre del organismo emisor no reconocido."),
]
WARNINGS = [
    ("EMISION_ANTIGUA", "El documento fue emitido hace más de 60 días."),
    ("NOMBRE_DIFIERE", "El nombre difiere levemente (posible tilde u orden de apellidos)."),
    ("FORMATO_NO_PDF", "Formato de imagen; se recomienda PDF para mejor trazabilidad."),
    ("FONDO_FOTO_INVALIDO", "La fotografía tiene fondo de color; SIGA exige fondo blanco."),
]
INFOS = [
    ("DOC_RECONOCIDO", "Tipo de documento reconocido correctamente."),
    ("QR_VERIFICADO", "Código QR verificado contra el registro emisor."),
    ("RESOLUCION_OK", "Resolución 240 DPI, sin señales de alteración."),
]


class IAReviewer(ABC):
    @abstractmethod
    def revisar(self, contenido: bytes, filename: str, doc: Documento, contexto: str) -> ResultadoIA: ...


class SimulatedReviewer(IAReviewer):
    """Determinista por archivo: mismo archivo → mismo resultado."""

    def revisar(self, contenido: bytes, filename: str, doc: Documento, contexto: str) -> ResultadoIA:
        seed = int(hashlib.sha256(contenido[:4096] + filename.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        hallazgos = [Hallazgo("info", *rng.choice(INFOS))]
        roll = rng.random()
        if roll < 0.40:
            resultado = "validado"
        elif roll < 0.80:
            resultado = "con_observaciones"
            hallazgos.append(Hallazgo("warning", *rng.choice(WARNINGS)))
        else:
            resultado = "con_errores"
            hallazgos.append(Hallazgo("error", *rng.choice(ERRORES)))
            if rng.random() < 0.5:
                hallazgos.append(Hallazgo("warning", *rng.choice(WARNINGS)))
        return ResultadoIA(
            resultado=resultado,
            confianza=round(rng.uniform(0.82, 0.99), 3),
            campos_extraidos={"archivo": filename, "documento": doc.titulo},
            hallazgos=hallazgos,
        )


class ClaudeReviewer(IAReviewer):
    """Adaptador real: envía el documento a la API de Claude y estructura el resultado."""

    PROMPT = (
        "Eres un revisor de documentos de acreditación minera en Chile. Analiza el documento "
        "adjunto para el requisito '{titulo}' (contexto: {contexto}). Responde SOLO un JSON con: "
        '{{"resultado": "validado|con_observaciones|con_errores", "confianza": 0.0-1.0, '
        '"campos_extraidos": {{...}}, "hallazgos": [{{"tipo": "error|warning|info", '
        '"codigo": "SNAKE_CASE", "mensaje": "..."}}]}}. Verifica: legibilidad, firma/timbre, '
        "fechas de emisión y vencimiento, y coherencia de identidad."
    )

    def revisar(self, contenido: bytes, filename: str, doc: Documento, contexto: str) -> ResultadoIA:
        import base64
        import json
        import urllib.request

        media = "application/pdf" if filename.lower().endswith(".pdf") else "image/jpeg"
        bloque = {
            "type": "document" if media == "application/pdf" else "image",
            "source": {"type": "base64", "media_type": media,
                       "data": base64.b64encode(contenido).decode()},
        }
        body = {
            "model": "claude-sonnet-5",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": [
                bloque,
                {"type": "text", "text": self.PROMPT.format(titulo=doc.titulo, contexto=contexto)},
            ]}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        texto = data["content"][0]["text"]
        parsed = json.loads(texto[texto.index("{"): texto.rindex("}") + 1])
        return ResultadoIA(
            resultado=parsed.get("resultado", "con_observaciones"),
            confianza=float(parsed.get("confianza", 0.8)),
            campos_extraidos=parsed.get("campos_extraidos", {}),
            hallazgos=[Hallazgo(h["tipo"], h["codigo"], h["mensaje"])
                       for h in parsed.get("hallazgos", [])],
        )


def get_reviewer() -> IAReviewer:
    if settings.ia_backend == "claude" and settings.anthropic_api_key:
        return ClaudeReviewer()
    return SimulatedReviewer()


def ejecutar_revision(
    db: Session, archivo: DocumentoArchivo, doc: Documento,
    contenido: bytes, contexto: str,
) -> IaReview:
    """Ejecuta la revisión (síncrona en dev; en producción iría a un worker)."""
    review = IaReview(
        company_id=doc.company_id, archivo_id=archivo.id, context=contexto,
        status="processing", started_at=datetime.now(timezone.utc),
    )
    db.add(review)
    db.flush()
    try:
        r = get_reviewer().revisar(contenido, archivo.filename, doc, contexto)
        review.resultado = r.resultado
        review.confianza = r.confianza
        review.campos_extraidos = r.campos_extraidos
        review.status = "done"
        for h in r.hallazgos:
            db.add(IaHallazgo(review_id=review.id, tipo=h.tipo, codigo=h.codigo, mensaje=h.mensaje))
    except Exception as e:  # noqa: BLE001
        review.status = "failed"
        review.error = str(e)[:500]
    review.finished_at = datetime.now(timezone.utc)
    archivo.ia_review_id = review.id
    return review
