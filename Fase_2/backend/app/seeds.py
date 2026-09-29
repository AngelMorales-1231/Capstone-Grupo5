"""Datos semilla idempotentes: catálogos, admin y empresa demo."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Company, DocEjemplo, Faena, FaenaPlataforma, RequisitoTemplate, User,
)
from .security import hash_password

FAENAS = [
    ("Los Pelambres", "Antofagasta Minerals", "AMSA", "Coquimbo", "#1f6f54", -31.72, -70.49),
    ("Minera Centinela", "Antofagasta Minerals", "AMSA", "Antofagasta", "#8a5a00", -22.97, -69.09),
    ("Zaldívar", "Antofagasta Minerals", "AMSA", "Antofagasta", "#7a3030", -24.20, -69.05),
    ("Antucoya", "Antofagasta Minerals", "AMSA", "Antofagasta", "#3a5f8a", -22.63, -69.85),
    ("Candelaria", "Lundin Mining", "Lundin", "Atacama", "#795c9c", -27.51, -70.29),
    ("Caserones", "Lundin Mining", "Lundin", "Atacama", "#2f7d8a", -28.03, -69.28),
    ("Andina", "Codelco", "Codelco", "Valparaíso", "#b04a17", -33.15, -70.25),
    ("El Teniente", "Codelco", "Codelco", "O'Higgins", "#5f7a30", -34.09, -70.35),
]

PLATAFORMAS = {
    "Los Pelambres": ["SIGA", "DIRECTIC", "SGES", "Academia MLP", "EMSIPOR"],
    "Minera Centinela": ["SIGA", "INTELICONTROL"],
    "Zaldívar": ["WEBCONTROL"],
    "Antucoya": ["SIGA", "INTELICONTROL"],
    "Candelaria": ["WEBCONTROL"],
    "Caserones": ["WEBCONTROL", "SUCAL"],
    "Andina": ["META CONTRATAS", "WEBCONTROL"],
    "El Teniente": ["META CONTRATAS", "WEBCONTROL"],
}

REQ_EMPRESA = [
    ("Contrato de Servicio", True, "contrato", "legal", None),
    ("Estrategias de Control SSO", True, None, "certificacion", None),
    ("Declaración Representante Legal", True, None, "legal", None),
    ("Reunión de Arranque", True, None, "certificacion", None),
    ("Programa de Trabajo SSO", True, "programa", "certificacion", 12),
    ("Copia Carta Inicio Actividades SERNAGEOMIN", True, None, "legal", None),
    ("Matriz de Riesgo", True, None, "tecnico", 12),
    ("Procedimiento de Emergencia", True, None, "tecnico", None),
    ("Certificado Ley 16.744", True, "mutual", "legal", 1),
    ("Jornada Excepcional de Trabajo", False, None, "legal", None),
]
REQ_PERSONAL = [
    ("Cédula de Identidad", True, "licencia", "legal", None),
    ("Contrato de Trabajo", True, "contrato", "legal", None),
    ("Anexo Contrato", True, "contrato", "legal", None),
    ("Certificado de Salud y Examen de Altura", True, "altura", "medico", 12),
    ("Inducción Hombre Nuevo", True, "induccion", "capacitacion", 24),
    ("Reglamento Interno", True, "rioshs", "legal", None),
    ("Certificado de Estudios", True, "estudios", "legal", None),
    ("Finiquito Anterior", False, "finiquito", "legal", None),
    ("Consentimiento Alcohol y Drogas", True, "consentimiento", "legal", 12),
    ("ODI Curso COVID", True, "odi", "capacitacion", None),
    ("Certificado de Residencia", False, "residencia", "legal", 6),
    ("IRL Mina y Chancado", True, "irlmina", "capacitacion", 12),
    ("Manejo Defensivo", True, "curso", "capacitacion", 24),
]
REQ_EQUIPO = [
    ("Permiso de Circulación", True, "circulacion", "legal", 12),
    ("SOAP", True, "soap", "legal", 12),
    ("Revisión Técnica", True, "revtec", "tecnico", 12),
    ("GPS", True, "gps", "tecnico", None),
    ("Certificado Incorporación Multiflota GPS MLP", True, "gps", "tecnico", None),
    ("Certificado de Láminas", True, None, "tecnico", None),
    ("Inspección Visual", True, "checklist", "tecnico", 6),
    ("Certificado de Mantenciones", True, "mantencion", "tecnico", 6),
    ("Certificado de Operatividad", True, "mantencion", "tecnico", 12),
    ("Certificado Barra Antivuelcos", False, "barra", "tecnico", None),
]
REQ_EMSIPOR = [
    ("Solicitud AIC Mina", True, None, "certificacion", None, "SIGA"),
    ("Fotografía Trabajador", True, None, "certificacion", None, "SIGA"),
    ("Licencia Municipal de Conducir", True, "licencia", "legal", None, "SIGA"),
    ("Hoja de Vida del Conductor", True, None, "legal", 1, "SIGA"),
    ("Psicosensométrico Riguroso", True, "psico", "medico", 24, "DIRECTIC"),
    ("Curso Manejo Defensivo y Alta Montaña", True, "curso", "capacitacion", 24, "Academia MLP"),
    ("Examen Práctico Mina", True, None, "capacitacion", None, "EMSIPOR"),
    ("Anexo Faena", True, None, "legal", None, "EMSIPOR"),
    ("Anexo Mina", True, None, "legal", None, "EMSIPOR"),
]

EJEMPLOS = [
    ("contrato", "Contrato de Trabajo", "Código del Trabajo",
     [["Empleador", "Razón social y RUT"], ["Trabajador", "Nombre y RUT"]],
     "Debe estar firmado por ambas partes."),
    ("altura", "Certificado de Salud y Examen de Altura", "DS 594 / Estándar MLP",
     [["Resultado", "APTO / NO APTO"], ["Validez", "12 meses"]],
     "Solo centros médicos homologados por la faena."),
    ("induccion", "Inducción Hombre Nuevo", "Estándar del mandante",
     [["Vigencia", "24 meses"]], None),
    ("soap", "Seguro Obligatorio SOAP", "Ley 18.490",
     [["Vigencia", "Anual"], ["Patente", "Debe coincidir"]], None),
    ("revtec", "Revisión Técnica", "MTT",
     [["Vigencia", "Anual"]], None),
    ("psico", "Examen Psicosensométrico Riguroso", "Estándar EMSIPOR",
     [["Vigencia", "2 años"]], None),
    ("licencia", "Licencia Municipal de Conducir", "Ley de Tránsito",
     [["Clase", "Según tipo de vehículo"]], None),
    ("mutual", "Certificado Ley 16.744", "Mutualidad",
     [["Vigencia", "30 días desde emisión"]], "Se renueva mensualmente."),
    ("programa", "Programa de Trabajo SSO", "Estándar del mandante",
     [["Período", "Anual"]], None),
    ("rioshs", "Reglamento Interno de Orden, Higiene y Seguridad", "DT",
     [["Registro", "Dirección del Trabajo"]], None),
    ("estudios", "Certificado de Estudios", "MINEDUC",
     [["Nivel", "Según cargo"]], None),
    ("finiquito", "Finiquito Anterior", "Código del Trabajo",
     [["Firma", "Ministro de fe"]], None),
    ("consentimiento", "Consentimiento Alcohol y Drogas", "Estándar del mandante",
     [["Vigencia", "12 meses"]], None),
    ("residencia", "Certificado de Residencia", "Junta de vecinos / notarial",
     [["Vigencia", "6 meses"]], None),
    ("odi", "ODI - Obligación de Informar", "DS 40 art. 21",
     [["Firma", "Trabajador y empleador"]], None),
    ("irlmina", "IRL Mina y Chancado", "Estándar MLP",
     [["Vigencia", "12 meses"]], None),
    ("curso", "Certificado de Curso", "Organismo capacitador",
     [["Vigencia", "Según curso"]], None),
    ("circulacion", "Permiso de Circulación", "Municipalidad",
     [["Vigencia", "Anual"]], None),
    ("gps", "Certificado GPS", "Proveedor homologado",
     [["Proveedor", "Wisetrack / MiFlota / SafeTrack"]], None),
    ("checklist", "Inspección Visual / Checklist", "Estándar faena",
     [["Vigencia", "6 meses"]], None),
    ("mantencion", "Certificado de Mantenciones", "Taller homologado",
     [["Vigencia", "6 meses"]], None),
    ("barra", "Certificado Barra Antivuelcos", "Estándar MLP (camionetas)",
     [["Instalador", "Taller certificado"]], None),
]


def run(db: Session) -> None:
    if not db.scalar(select(Faena.id).limit(1)):
        for nombre, mandante, grupo, region, color, lat, lng in FAENAS:
            f = Faena(nombre=nombre, mandante=mandante, grupo=grupo,
                      region=region, color=color, lat=lat, lng=lng)
            db.add(f)
            db.flush()
            for p in PLATAFORMAS.get(nombre, []):
                db.add(FaenaPlataforma(faena_id=f.id, nombre=p))
        db.flush()

    if not db.scalar(select(DocEjemplo.clave).limit(1)):
        for clave, nombre, ref, campos, tip in EJEMPLOS:
            db.add(DocEjemplo(clave=clave, nombre=nombre, referencia=ref,
                              campos_clave=campos, notas=[], tip=tip))
        db.flush()

    if not db.scalar(select(RequisitoTemplate.id).limit(1)):
        contador = 0

        def agregar(ambito, filas):
            nonlocal contador
            for fila in filas:
                titulo, oblig, ejemplo, tipo, vig = fila[:5]
                plataforma = fila[5] if len(fila) > 5 else None
                contador += 1
                db.add(RequisitoTemplate(
                    ambito=ambito, titulo=titulo, codigo=f"REQ-{contador:03d}",
                    tipo=tipo, obligatorio=oblig, ejemplo_clave=ejemplo,
                    vigencia_meses=vig, plataforma=plataforma,
                ))

        agregar("empresa", REQ_EMPRESA)
        agregar("personal", REQ_PERSONAL)
        agregar("equipo", REQ_EQUIPO)
        agregar("emsipor", REQ_EMSIPOR)
        db.flush()

    if not db.scalar(select(User).where(User.email == "admin@acredittia.cl")):
        db.add(User(email="admin@acredittia.cl",
                    password_hash=hash_password("Admin2026!"),
                    role="admin", company_id=None, status="approved"))

    if not db.scalar(select(User).where(User.email == "demo@acredittia.cl")):
        demo = Company(nombre="DEMO SpA", rut="76.111.222-8",
                       email="demo@acredittia.cl", status="approved")
        db.add(demo)
        db.flush()
        db.add(User(email="demo@acredittia.cl",
                    password_hash=hash_password("Demo2026!"),
                    role="company", company_id=demo.id, status="approved"))

    db.commit()
