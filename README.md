# Acredittia — Administración del Negocio (Back Office)

Plataforma SaaS diseñada para centralizar la supervisión operativa, la gestión administrativa de empresas contratistas, suscripciones, facturación y el monitoreo automatizado de procesos críticos.

* **Asignatura:** CAPSTONE_300D — Ingeniería en Informática (Duoc UC)
* **Docente:** Claudia Reinoso
* **Equipo:** Grupo 5

## Equipo de Desarrollo

| Integrante | Rol | Principales Aportes |
| :--- | :--- | :--- |
| **Ángel Morales** | Scrum Master y Data Engineer | Modelo de datos en PostgreSQL, funciones PL/pgSQL y Manual Técnico para el Programador |
| **Paula Molina** | Backend Developer (REST API) | Desarrollo de API REST (Consola Interna), Webhook de pagos y Manual de Usuario y Operación |
| **Antonia Valencia** | Backend Developer y QA | Orquestación en Docker (Celery/Redis), pruebas automatizadas y Guía de Instalación |

---

## Stack Tecnológico y Validación

* **Backend:** Python 3.12, FastAPI, Uvicorn y SQLAlchemy (39 endpoints, autenticación Bearer JWT y Webhook de pagos con firma criptográfica HMAC-SHA256)
* **Base de Datos:** PostgreSQL 16 (funciones PL/pgSQL y políticas de seguridad RLS)
* **Procesamiento Asíncrono:** Celery (Beat & Worker) y Redis 7 Alpine
* **Infraestructura:** Docker y Docker Compose
* **Pruebas Automatizadas:** Suite de 305 pruebas unitarias y de integración en Pytest + colección de 39 pruebas automatizadas en Postman

---

## Documentación y Evidencias del Proyecto

### Fase 1 — Definición del Proyecto
* **Evidencias Grupales:**
  * [1.4 Formativa Fase 1](<./Fase 1/Evidencias Grupales/1.4_APT122_FormativaFase1.docx>)
  * [1.5 Definición Proyecto APT (Español)](<./Fase 1/Evidencias Grupales/1.5_GuiaEstudiante_Fase 1_Definicion Proyecto APT (Español).docx>)
  * [Presentación Proyecto (Fase 1)](<./Fase 1/Evidencias Grupales/Presentación Proyecto.pptx>)
* **Evidencias Individuales:** [Ver carpeta Fase 1/Evidencias Individuales](<./Fase 1/Evidencias Individuales>) *(autoevaluaciones 1.1, diarios de reflexión 1.2 y autoevaluaciones 1.3)*

### Fase 2 — Desarrollo, Documentación Técnica y Evidencias
* **Evidencias Grupales:**
  * [2.4 Desarrollo Proyecto APT (Español)](<./Fase 2/Evidencias Grupales/2.4_GuiaEstudiante_Fase 2_DesarrolloProyecto APT (Español).docx>)
  * [2.6 Informe Final Proyecto APT (Español)](<./Fase 2/Evidencias Grupales/2.6_GuiaEstudiante_Fase 2_Informe Final Proyecto APT (Español).docx>)
* **Presentaciones de Avance y Proyecto:**
  * [Presentación Proyecto (Fase 2)](<./Fase 2/Evidencias Proyecto/Presentación Proyecto.pptx>)
  * [Presentación Avance 1](<./Fase 2/Evidencias Proyecto/Presentacion Avance.pptx>)
  * [Presentación Avance 2](<./Fase 2/Evidencias Proyecto/Presentación Avance 2.pptx>)
* **Documentación Técnica y Manuales:**
  * [Manual Técnico para el Programador](<./Fase 2/Evidencias Proyecto/Evidencias de documentación/Manual Técnico para el Programador.docx>)
  * [Manual de Usuario y Operación](<./Fase 2/Evidencias Proyecto/Evidencias de documentación/Manual de Usuario y Operación.docx>)
  * [Guía de Instalación, Configuración y Despliegue](<./Fase 2/Evidencias Proyecto/Evidencias de documentación/Guía de Instalación, Configuración y Despliegue.docx>)
  * [Evidencias de la Documentación](<./Fase 2/Evidencias Proyecto/Evidencias de documentación/Evidencias de la Documentacion.docx>)
* **Evidencias de Sistema:**
  * [Evidencias de Sistema — Aplicación](<./Fase 2/Evidencias Proyecto/Evidencias de sistema/Aplicación/Evidencias de Sistema Aplicacion.docx>)
* **Evidencias Individuales:** [Ver carpeta Fase 2/Evidencias Individuales](<./Fase 2/Evidencias Individuales>) *(diarios de reflexión 2.1 y autoevaluaciones 2.2)*

### Fase 3 — Cierre y Presentación Final
* **Evidencias Grupales:**
  * **Video de la presentación:** [Ver 3.2_Video_Grupo5.mp4 en OneDrive](https://duoccl0-my.sharepoint.com/:v:/g/personal/an_moralesm_duocuc_cl/IQAic6kjp-veSYB248gxLWQ0AdrEJYVdiJxdHxw_RCtxfHM?e=tXLeW0) *(alojado en OneDrive de Duoc UC por superar el límite de 100 MB de GitHub — 172 MB)*
  * [Diapositivas en PDF con Tabla de Tramos — 3.2_Presentacion_Grupo5.pdf](<./Fase 3/Evidencias Grupales/3.2_Presentacion_Grupo5.pdf>)
  * [Transcripción — 3.2_Transcripcion_Grupo5.docx](<./Fase 3/Evidencias Grupales/3.2_Transcripcion_Grupo5.docx>)
  * [Presentación Final del proyecto (Español).pptx](<./Fase 3/Evidencias Grupales/Presentación Final del proyecto (Español).pptx>)
* **Evidencias Individuales:** [Ver carpeta Fase 3/Evidencias Individuales](<./Fase 3/Evidencias Individuales>) *(diarios de reflexión 3.1 y autoevaluaciones 3.2)*