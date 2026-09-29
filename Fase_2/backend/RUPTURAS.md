# Rupturas de contrato — backend Acredittia v1.1

Cambios **incompatibles** introducidos al alinear los ocho routers originales
(`auth`, `admin`, `faenas`, `contratos`, `sujetos`, `documentos`, `alertas`,
`dashboard`) con la especificación v1.1. Es ruptura limpia: **no se conservan
alias deprecados**. Todo lo listado aquí exige tocar el frontend.

Prefijo de todas las rutas: `/api/v1`.

---

## 0. Ruptura transversal: envoltura de listados

**TODOS** los listados devuelven ahora la envoltura estándar `sobre()`:

```jsonc
// antes (variaba por endpoint)
{ "items": [...], "total": 12 }
{ "items": [...], "page": 1, "page_size": 25, "total": 12 }
{ "items": [...] }

// ahora, siempre
{ "items": [...], "page": 1, "page_size": 25, "total": 12, "total_pages": 1 }
```

Consecuencias:

- Los listados que devolvían un array plano o un dict sin `page` ahora paginan.
  **Un cliente que no envíe `page`/`page_size` recibe solo los primeros 25
  elementos** (`PAGE_SIZE_DEFAULT`), no la colección completa.
- `page_size` está limitado a `PAGE_SIZE_MAX` (100). Valores mayores → **422**.
- Todos aceptan `?sort=campo` / `?sort=-campo` y `?search=`.

Endpoints afectados por esto (además de sus cambios propios):
`GET /admin/companies`, `GET /admin/requisitos/templates`, `GET /faenas`,
`GET /faenas/{id}/plataformas`, `GET /catalogo/requisitos-templates`,
`GET /catalogo/ejemplos`, `GET /catalogo/laboratorios`, `GET /catalogo/talleres`,
`GET /catalogo/proveedores-gps`, `GET /contratos`,
`GET /contratos/{id}/documentos`, `GET /contratos/{id}/personal`,
`GET /contratos/{id}/equipos`, `GET /contratos/{id}/alertas`,
`GET /contratos/{id}/historial`, `GET /personal`, `GET /equipos`,
`GET /documentos`, `GET /alertas`, `GET /dashboard/cumplimiento-contratos`,
`GET /dashboard/actividad`, `GET /dashboard/proximos-vencimientos`.

**Excepciones deliberadas** (conjuntos cerrados, no paginan):
`GET /catalogo/tipos-equipo`, `GET /catalogo/requisitos-terreno`
(devuelven `{items, total}`), y `GET /contratos/{id}/matriz`, que tiene su propia
paginación por filas (`total_filas`, tope `MATRIZ_FILAS_MAX`).

## 0.1 Ruptura transversal: PATCH con cuerpo vacío

Un `PATCH` sin ningún campo devolvía 200 y no hacía nada. Ahora devuelve
**400 `SIN_CAMBIOS`**. Afecta a `PATCH /auth/me`, `/admin/faenas/{id}`,
`/admin/requisitos/templates/{id}`, `/personal/{id}`, `/equipos/{id}`,
`/contratos/{id}`, `/documentos/{id}`, `/alertas/{id}`,
`/personal/{sid}/licencia-interna`.

## 0.2 Ruptura transversal: alcance del contract_admin

Los listados y KPI que antes devolvían datos de toda la empresa ahora se acotan
al contrato del `contract_admin` (`contrato_scope`). Pedir explícitamente otro
contrato devuelve **404** (no 403), para no revelar su existencia. Afecta en
particular a `GET /dashboard/kpis`, `/dashboard/acreditaciones-estado`,
`/dashboard/cumplimiento-contratos`, `/dashboard/actividad`,
`/dashboard/proximos-vencimientos`, `/dashboard/tendencia`, `GET /alertas`,
`GET /documentos`, `GET /personal`, `GET /equipos`, `GET /contratos`.

---

## 1. `auth`

| Antes | Ahora |
|---|---|
| `POST /auth/login` no miraba `users.activo` | 403 `CUENTA_DESACTIVADA` si `activo = false` |
| `POST /auth/refresh` no miraba `users.activo` | 401 `REFRESH_INVALIDO` si `activo = false` |
| `GET /auth/me` → `{id, email, role, company}` | añade `nombre`, `activo`, `contrato_id`, `contrato {id,nombre,codigo}`, `last_login_at` |
| `login.user` con la forma antigua | misma forma ampliada que `GET /auth/me` |

`contrato` solo viene poblado para `role = contract_admin`; para el resto es
`null`.

**Nuevos** (no rompen nada):

- `POST /auth/password/forgot` — `{email}`. Responde **siempre 200** con el mismo
  mensaje, exista o no la cuenta. En desarrollo (`JWT_SECRET` empieza por `dev-`)
  incluye `_dev_token` y `_dev_nota`; en producción esas claves **no aparecen**,
  así que el cliente no debe depender de ellas.
- `POST /auth/password/reset` — `{token, password}`. 400 `TOKEN_INVALIDO`,
  400 `PASSWORD_DEBIL`. Revoca **todos** los refresh tokens del usuario:
  tras un reset hay que volver a hacer login.
- `PATCH /auth/me` — `{nombre?, email?, password?, password_actual?, refresh_token?}`.
  Cambiar `password` exige `password_actual` (401 `PASSWORD_ACTUAL_REQUERIDA` /
  `PASSWORD_ACTUAL_INVALIDA`) y revoca los demás refresh tokens. Email duplicado
  → 409 `EMAIL_EN_USO`. **Enviar `refresh_token` (el de la sesión en curso)
  evita que esa sesión se cierre**; sin él se revocan todos.

---

## 2. `admin`

| Antes | Ahora |
|---|---|
| `GET /admin/companies` → `{items, total}` sin paginar | `sobre()`; nuevo filtro `?es_demo=`; cada fila añade `es_demo`, `approved_at`, `cumplimiento_pct`, `docs_ok`, `docs_total` |
| `GET /admin/stats` → `{empresas_total, pendientes, aprobadas, sujetos_total}` | añade `rechazadas`, `contratos_total`, `faenas_total`, `faenas_activas`, `plantillas_activas`, `personal {total, acreditados}`, `equipos {total, acreditados}`, `documentos_total`, `documentos_ok`, `cumplimiento_promedio`, `cumplimiento_global_pct`, `empresas_medidas`. `sujetos_total` se conserva |

`cumplimiento_promedio` es la media **simple** por empresa aprobada con
documentos obligatorios; `cumplimiento_global_pct` es la media **ponderada** por
documentos. Son dos números distintos a propósito.

**Nuevos**:

- `GET /admin/companies/{id}` — ficha con `stats {usuarios, usuarios_activos, contratos, contratos_vigentes, personal, equipos, documentos, alertas_activas, cumplimiento_pct}`.
- `POST /admin/faenas` · `PATCH /admin/faenas/{id}` — 409 `FAENA_DUPLICADA` por nombre.
- `POST /admin/faenas/{id}/plataformas` — 409 `PLATAFORMA_DUPLICADA` si el nombre ya existe en esa faena.
- `GET /admin/requisitos/templates` — paginado; filtros `ambito`, `faena_id`, `activo`, `search`. **Devuelve también las inactivas** (a diferencia de `GET /requisitos/templates`).
- `POST /admin/requisitos/templates` — 409 `CODIGO_DUPLICADO`.
- `PATCH /admin/requisitos/templates/{id}` — respuesta con `documentos_instanciados` y `nota`.
- `DELETE /admin/requisitos/templates/{id}` — **borrado en blando** (`activo=false`). No borra documentos ya instanciados; lo dice en `nota` y devuelve `documentos_instanciados`.
- `POST /admin/companies/{id}/reset-demo` — 409 `NO_ES_DEMO` si `es_demo=false`. Devuelve `{borrados, creados, documentos_por_estado, nota}`.
  **La tabla `actividad` NO se borra** (es append-only por trigger, SQLSTATE 55000): el historial anterior al reset sobrevive.
  Los `users` con `role=contract_admin` de los contratos borrados **sí** desaparecen (FK `ON DELETE CASCADE`); se informan en `borrados.usuarios_contract_admin`.

En las plantillas de `/admin/...` la clave del identificador es **`id`**, no
`template_id` como en `GET /requisitos/templates`.

---

## 3. `faenas` y catálogos

| Antes | Ahora |
|---|---|
| `GET /faenas` → `{items, total}` | `sobre()`; nuevos filtros `sector`, `activa`, `mandante`, `search`; cada fila añade `logo_url` |
| `stats` de la faena: `{contratos, personal, equipos}` | añade `personal_acreditado`, `equipos_acreditados`, `docs_total`, `docs_ok`, `cumplimiento_pct` |
| `GET /faenas/{id}/plataformas` → `{items:[{id,nombre,descripcion,url,nota}]}` | `sobre()`; cada fila añade `faena_id`, `orden` y **`acceso {estado, nota, solicitado_at, habilitado_at}`** con el estado de la empresa (`sin_acceso` si no hay fila) |
| `GET /catalogo/requisitos-templates` → `{items}` | `sobre()`; nuevo filtro `faena_id`; añade `faena_id` a cada fila |
| `GET /catalogo/ejemplos/{clave}` → `{clave,nombre,referencia,campos_clave,notas,tip}` | añade `pdf_url`, `tiene_campos_clave`, `tiene_notas` |
| `GET /catalogo/tipos-equipo` → `{items}` | añade `total` |

**Nuevos**:

- `GET /faenas/{id}` — ficha con branding, coordenadas, `plataformas[]` (con
  acceso) , `stats` de la empresa en esa faena y `requisitos_faena`.
- `PATCH /faenas/{id}/plataformas/{pid}/acceso` — `{estado, nota?}`. Upsert en
  `company_faena_plataformas`. `estado='solicitada'` fija `solicitado_at`;
  `estado='activa'` fija `habilitado_at`; **las marcas no se borran al salir del
  estado**. Respuesta con `creado` y `estado_anterior`. Requiere rol `company` o
  `admin` (un `contract_admin` recibe 403 `ROL_INSUFICIENTE`).
- `GET /catalogo/ejemplos` — biblioteca paginada. En el **listado** no vienen
  `campos_clave` ni `notas` (solo `tiene_campos_clave` / `tiene_notas`); para
  obtenerlos hay que abrir `GET /catalogo/ejemplos/{clave}`.
- `GET /catalogo/requisitos-terreno` — filtros `ambito=conductor|equipo`, `nivel`.
  **No pagina**: `{items, total}`.
- `GET /catalogo/laboratorios` · `GET /catalogo/talleres` ·
  `GET /catalogo/proveedores-gps` — paginados, filtro opcional `faena_id`, que
  **incluye además los proveedores generales** (`faena_id IS NULL`).

---

## 4. `contratos`

| Antes | Ahora |
|---|---|
| `GET /contratos` → `{items, total}` | `sobre()`; nuevos `search`, `sort`; 400 `ESTADO_INVALIDO` con estado desconocido |
| `POST /contratos` no aceptaba `ia_review_id` | acepta `ia_review_id` opcional → se guarda en `origen_ia_review_id` (404 si el job no es de la empresa). `documentos_creados` se conserva |
| `_out` del contrato | añade `origen_ia_review_id` y `faena.region` |
| `GET /contratos/{id}/documentos` → `{items}` | `sobre()` |
| `DELETE /contratos/{id}` → `{ok, sujetos_eliminados}` | añade `archivos_eliminados` (y ahora **purga los blobs** de los sujetos, no solo las filas) |

**Nuevos**:

- `GET /contratos/{id}/personal` — mismos filtros y formato que `GET /personal`.
- `GET /contratos/{id}/equipos` — mismos filtros y formato que `GET /equipos`.
- `GET /contratos/{id}/alertas` — mismo formato de fila que `GET /alertas`.
- `GET /contratos/{id}/historial` — mismo formato de fila que `GET /actividad`.
- `POST /contratos/analizar` — `{blob_path, filename}` → **202**
  `{job_id, status, context, nota}`. Alias de `POST /ia/extraer-contrato`.
  **No crea el contrato**: el `job_id` se pasa después como `ia_review_id`.
- `GET /contratos/{id}/matriz` — matriz dispersa sujeto × requisito.
  Params `tipo=personal|equipo`, `incluir_opcionales`, `cargo_id`, `page`,
  `page_size` (tope `MATRIZ_FILAS_MAX` = 200).

```jsonc
{
  "tipo": "personal", "contrato_id": "…", "incluir_opcionales": false,
  "columnas": [ { "titulo": "Contrato de Trabajo", "obligatorio": true, "ambito": "personal" } ],
  "filas": [ {
    "sujeto_id": "…", "nombre": "…", "rut": "…", "cargo": "…",
    "cumplimiento_pct": 78,
    "celdas": [ { "col": 0, "estado_calc": "ok", "vence": "2026-11-30" } ]
  } ],
  "page": 1, "page_size": 25, "total_filas": 42, "total_pages": 2
}
```

**Contrato de la matriz que el frontend debe respetar:**

- La matriz es **dispersa**. Una celda **ausente** significa «el requisito no
  aplica a este sujeto», que **no es lo mismo que `falta`**. No se emiten celdas
  nulas: hay que renderizar el hueco (`—`), no un incumplimiento.
- `celdas[].col` es el **índice** en `columnas`, y las celdas vienen ordenadas
  por `col`.
- El orden de columnas es estable y determinista: ámbito
  (`empresa`, `personal`, `equipo`, `emsipor`), luego obligatorios primero, luego
  título alfabético. Es el mismo orden que la exportación a Excel del recurso
  `matriz`, así que la columna N de la pantalla es la columna N del fichero.
- Las columnas son las de los sujetos **de la página**, no la plantilla teórica:
  al cambiar de página el conjunto de columnas puede variar.

---

## 5. `sujetos` (`/personal`, `/equipos`)

| Antes | Ahora |
|---|---|
| `GET /personal` / `GET /equipos` → `{items, page, page_size, total}` con `page_size` libre | `sobre()` (añade `total_pages`); `page_size` limitado a 100; `sort` disponible |
| filtros: `contrato_id`, `estado`, `search` | **personal**: añade `faena_id`, `cargo_id`, `cargo`, `es_conductor`. **equipos**: añade `faena_id`, `tipo_equipo` |
| `POST /personal` aceptaba solo `cargo` como texto | acepta **`cargo_id`** (recomendado) o `cargo` como texto; el texto se resuelve/crea con `checklist.resolver_cargo`. 400 `CARGO_INEXISTENTE` si el `cargo_id` no es de la empresa |
| `POST /personal` creaba EMSIPOR solo si `es_conductor` | lo decide `checklist.requiere_emsipor` (cargo **o** override del sujeto) |
| respuesta del alta: `{..., documentos_creados}` | añade **`cargo_creado`** (bool) y `expediente_emsipor_creado` (bool) |
| `PATCH /personal/{id}` aceptaba `marca`, `modelo`, `anio`, `tipo_equipo` | **cuerpo separado por tipo**: `/personal` solo `{nombre, cargo_id, cargo, es_conductor}`; `/equipos` solo `{nombre, marca, modelo, anio, tipo_equipo}`. Los campos del otro tipo se **ignoran** silenciosamente (Pydantic) |
| `PATCH /personal/{id}` no recalculaba EMSIPOR por cargo | acepta `cargo_id` y crea el expediente si el cargo nuevo lo exige (`expediente_emsipor_creado`) |
| `_out` del sujeto: `contrato {id, nombre, faena}` | añade `cargo_id` y `contrato.faena_id` |
| `POST /personal/{id}/baja` → `{ok, estado}` | añade `id` |
| `DELETE /personal|equipos/{id}` → `{ok}` | añade `archivos_eliminados`; **ahora purga los blobs** (antes quedaban huérfanos en el storage) |
| `instanciar_docs` se llamaba sin contrato ni cargo | se pasa `contrato_plantilla_id` y `cargo_id`, así que **se aplican los overrides del contrato y los requisitos personalizados**. El checklist resultante puede tener más (o menos) documentos que antes |

**Nuevos**:

- `GET /personal/{id}/documentos` · `GET /equipos/{id}/documentos` →
  `{sujeto, items, documentos_emsipor, total, stats}`. No es `sobre()`: es el
  checklist completo del sujeto, que es un conjunto cerrado.

Cambiar el cargo **no reinstancia** el checklist de personal: los documentos ya
subidos siguen siendo válidos. Solo se recalcula el expediente EMSIPOR.

---

## 6. `documentos` — RUPTURA MAYOR: subida y descarga por SAS

Los archivos **ya no pasan por la API**. El flujo pasa de una llamada a tres.

### 6.1 Subida

```
ANTES
POST /documentos/{id}/archivos      Content-Type: multipart/form-data
                                    campo: file
→ 201 { archivo, documento, ia_review: { id, status, resultado, confianza,
                                         campos_extraidos, hallazgos } }
```

```
AHORA (tres pasos)
1) POST /documentos/{id}/upload-url        { filename, content_type?, size_bytes? }
   → 200 { upload_url, blob_path, expires_at, headers, method:"PUT",
           max_bytes, confirmar_en }

2) PUT <upload_url>                        cuerpo = bytes crudos del archivo
                                           headers = los de la respuesta anterior
   (en Azure va directo al blob; con STORAGE_BACKEND=local va a PUT /blobs/upload)

3) POST /documentos/{id}/archivos          { blob_path, filename }
   → 201 { archivo: { id, filename, blob_path, size_bytes, content_type, created_at },
           ia_review: { job_id, status: "queued", context },
           documento, nota }
```

Cambios de comportamiento que hay que asumir:

- **`POST /documentos/{id}/archivos` ya no es multipart.** Es JSON. Enviar
  `multipart/form-data` → 422.
- **La revisión IA es asíncrona.** La respuesta trae `ia_review.status = "queued"`
  y **no** `resultado`, `confianza`, `campos_extraidos` ni `hallazgos`. El
  documento sigue en `estado = "falta"` en esa respuesta. Hay que hacer polling en
  `GET /ia/revisiones/{job_id}`, que es donde aparece el veredicto, la fecha de
  vencimiento autocompletada y `accion_aplicada`.
- La clave del job pasa de `ia_review.id` a **`ia_review.job_id`** (misma
  convención que `/ia`).
- Errores nuevos: 400 `BLOB_NO_ENCONTRADO` (el `PUT` no llegó al storage),
  400 `BLOB_NO_PERMITIDO` (la ruta no empieza por el `company_id`; hay que usar la
  que devuelve `upload-url`), 409 `BLOB_YA_REGISTRADO`.
- `size_bytes` en `upload-url` es **declarativo**: el tamaño real se relee del
  storage al confirmar y ahí se vuelve a validar contra `MAX_UPLOAD_MB`.
- El `blob_path` lo calcula el servidor y **no es negociable**.

### 6.2 Descarga

```
ANTES  GET /documentos/{id}/archivos/{fid}/descarga   → 200 con el binario
                                                        (Content-Disposition)
AHORA  GET /documentos/{id}/archivos/{fid}/download-url
       → 200 { download_url, expires_at, filename, content_type, size_bytes }
```

**El endpoint `/descarga` está ELIMINADO** (404). Hay que pedir la URL y navegar
a ella. Caduca en `SAS_DOWNLOAD_TTL_MIN` minutos y no debe cachearse.

### 6.3 Resto de `documentos`

| Antes | Ahora |
|---|---|
| no existía `GET /documentos` | **nuevo** listado transversal (ver abajo) |
| `PATCH /documentos/{id}` → `doc_out` | añade `vence_derivado` (bool) y `nota`. Al fijar `estado='ok'` **sin** `vence` se deriva de `vigencia_meses` de la plantilla |
| `doc_out` | añade `template_id`, `archivos_count`, y `content_type` / `created_at` en cada archivo |
| `DELETE /documentos/{id}/archivos/{fid}` → `{ok}` | `{ok, documento}` con el estado recalculado |

`GET /documentos` — filtros `sujeto_id`, `contrato_id`, `estado`, `estado_calc`,
`obligatorio`, `es_emsipor`, `vence_antes`, `vence_despues`, `search` (título).
Cada fila lleva el **dueño resuelto** y el conteo de archivos (no la lista):

```jsonc
{
  "id": "…", "titulo": "SOAP", "obligatorio": true,
  "estado": "ok", "estado_calc": "porvenc", "vence": "2026-09-01",
  "dias_para_vencer": 29, "es_emsipor": false,
  "template_id": "…", "ejemplo_clave": "soap", "plataforma": "SIGA",
  "archivos_count": 1,
  "dueno": { "tipo": "equipo", "id": "…", "nombre": "Volvo FM 440",
             "rut": null, "patente": "BCDF12" },
  "contrato_id": "…"
}
```

`dueno.tipo` ∈ `trabajador | equipo | contrato`. `vence_antes` y `vence_despues`
son inclusivos y **descartan los documentos sin fecha**.

---

## 7. `alertas`

| Antes | Ahora |
|---|---|
| `GET /alertas` → `{items, page, page_size, total}` | `sobre()`; nuevos filtros `estado`, `contrato_id`, `sujeto_id`, `leida`, `search`; `sort` |
| fila de alerta | añade `leida_at` y `resuelta_at` (las marcas de tiempo, además de los booleanos) |
| `PATCH /alertas/{id}` aceptaba `{leida, resuelta}` | acepta también `estado` (`nueva|en_progreso|bloqueante|informativa|resuelta`). Fijar `estado='resuelta'` pone `resuelta_at`; cualquier otro estado la **limpia** |
| `GET /alertas/resumen` | añade `activas` |
| las mutaciones no dejaban rastro | `PATCH` y `POST /marcar-leidas` registran en `actividad` |

Una alerta **sin `contrato_id`** es de alcance empresa: un `contract_admin`
recibe 404 al pedirla.

---

## 8. `dashboard`

| Antes | Ahora |
|---|---|
| `GET /dashboard/actividad?limit=10` → `{items}` | `?page`/`?page_size`; `sobre()`. **`limit` ya no existe** (se ignora). Cada fila añade `entidad_tipo` y `entidad_id` |
| `GET /dashboard/cumplimiento-contratos` → `{items}` | `sobre()`; añade `faena_id` |
| `GET /dashboard/proximos-vencimientos?dias=30` → `{items}` | `sobre()` (`dias` se conserva); añade `sujeto_id` |
| `GET /dashboard/kpis` | añade `documentos {ok, total}` |
| `GET /dashboard/acreditaciones-estado` | añade `total` |

**Nuevo** `GET /dashboard/tendencia` — params `periodo=semana|mes|trimestre`
(por defecto `mes`), `desde`, `hasta`, `contrato_id`, `faena_id`
(400 `FILTRO_AMBIGUO` si se envían los dos).

```jsonc
{
  "periodo": "mes", "desde": "2025-08-03", "hasta": "2026-08-03",
  "contrato_id": null, "faena_id": null, "snapshots_leidos": 340,
  "actual":   { "fecha": "2026-08-02", "periodo_inicio": "2026-08-01",
                "cumplimiento_pct": 82, "docs_ok": 410, "docs_total": 500 },
  "anterior": { "fecha": "2026-07-31", "periodo_inicio": "2026-07-01",
                "cumplimiento_pct": 76, "docs_ok": 380, "docs_total": 500 },
  "delta_pct": 6,
  "serie": [ { "periodo_inicio": "…", "fecha": "…", "cumplimiento_pct": 76,
               "docs_ok": 380, "docs_total": 500,
               "personal_acreditados": 12, "personal_total": 15,
               "equipos_acreditados": 6, "equipos_total": 8,
               "alertas_criticas": 2 } ]
}
```

**Contrato importante:** si no hay al menos **dos** periodos con snapshots,
**`anterior` y `delta_pct` NO aparecen en la respuesta** y en su lugar viene
`nota` explicando por qué. El frontend debe comprobar la presencia de las claves
en vez de asumir `delta_pct = 0`. Con la serie vacía tampoco aparece `actual`.

De cada periodo se toma el **último** snapshot (medición puntual), no el promedio.
Con `faena_id` se **agregan** los contratos de la faena recalculando
`docs_ok / docs_total` (no promediando porcentajes).

---

## 9. Routers nuevos

### `licencia_interna` — `/personal/{sid}/licencia-interna`

- `GET` → `{sujeto, numero, estado, vence, dias_para_vencer, emsipor_estado,
  checklist[9 docs con su `plataforma`], resumen}`.
- `PATCH` → `{numero?, vence?}`. **`estado` no se acepta del cliente**: se deriva
  (`pendiente` sin número · `por_vencer` si vence en ≤30 días · `vigente` si más).
  Con número y sin fecha → `vigente`.
- `POST /reset` → borra los documentos `es_emsipor` del sujeto **y sus archivos y
  blobs**, los reinstancia en blanco y limpia número/vencimiento/estado.
  Devuelve `reset {documentos_eliminados, archivos_eliminados, documentos_creados}`.
  **Irreversible.** No toca el resto del checklist del trabajador.

Los tres devuelven **409 `NO_REQUIERE_EMSIPOR`** si el trabajador no lo necesita
según `checklist.requiere_emsipor` (cargo o override `es_conductor`).

Limitación conocida: el enum `lim_estado` no tiene valor «vencida», así que una
licencia ya caducada se devuelve como `por_vencer`; distínguelo comparando
`vence` con hoy.

### `blobs` — `/blobs` (solo `STORAGE_BACKEND=local`)

- `PUT /blobs/upload?blob_path=&exp=&sig=` — cuerpo crudo → 201 con cabeceras
  `x-blob-path` y `x-blob-size`.
- `GET /blobs/download?blob_path=&exp=&sig=&filename=` — el binario con
  `Content-Disposition` y `Cache-Control: no-store`.

**No requieren token: la firma ES la autorización**, igual que una SAS de Azure.
Firma inválida o caducada → 403 `FIRMA_INVALIDA`. Con
`STORAGE_BACKEND=azure` responden 400 `BACKEND_NO_LOCAL`, porque en Azure el
navegador va directo al blob y estas rutas no se usan.

El frontend **no debe construir estas URLs**: llegan ya firmadas en
`upload_url` / `download_url`.

---

## 10. Códigos de error nuevos

| Código | HTTP | Dónde |
|---|---|---|
| `CUENTA_DESACTIVADA` | 403 | `POST /auth/login` |
| `TOKEN_INVALIDO` | 400 | `POST /auth/password/reset` |
| `PASSWORD_DEBIL` | 400 | `POST /auth/password/reset`, `PATCH /auth/me` |
| `PASSWORD_ACTUAL_REQUERIDA` / `PASSWORD_ACTUAL_INVALIDA` | 401 | `PATCH /auth/me` |
| `EMAIL_EN_USO` | 409 | `PATCH /auth/me` |
| `SIN_CAMBIOS` | 400 | todos los `PATCH` |
| `FAENA_DUPLICADA` | 409 | `POST/PATCH /admin/faenas` |
| `PLATAFORMA_DUPLICADA` | 409 | `POST /admin/faenas/{id}/plataformas` |
| `AMBITO_INVALIDO` / `TIPO_INVALIDO` / `EJEMPLO_INEXISTENTE` | 400 | plantillas de `/admin` |
| `CODIGO_DUPLICADO` | 409 | plantillas de `/admin`, `POST/PATCH /contratos` |
| `NO_ES_DEMO` / `RESET_BLOQUEADO` / `CATALOGO_VACIO` | 409 | `POST /admin/companies/{id}/reset-demo` |
| `ESTADO_INVALIDO` | 400 | `/faenas/.../acceso`, `/contratos`, `/alertas`, `/documentos` |
| `CARGO_INEXISTENTE` | 400 | `POST/PATCH /personal` |
| `CARGO_SOLO_PERSONAL` | 400 | `GET /contratos/{id}/matriz` |
| `BLOB_NO_ENCONTRADO` / `BLOB_NO_PERMITIDO` / `BLOB_PATH_REQUERIDO` | 400 | `POST /documentos/{id}/archivos` |
| `BLOB_YA_REGISTRADO` | 409 | `POST /documentos/{id}/archivos` |
| `ARCHIVO_VACIO` / `ARCHIVO_MUY_GRANDE` / `EXTENSION_NO_PERMITIDA` | 400 | subida |
| `ESTADO_CALC_INVALIDO` / `RANGO_INVALIDO` | 400 | `GET /documentos` |
| `PERIODO_INVALIDO` / `FILTRO_AMBIGUO` | 400 | `GET /dashboard/tendencia` |
| `NO_REQUIERE_EMSIPOR` | 409 | `/personal/{sid}/licencia-interna` |
| `FIRMA_INVALIDA` | 403 | `/blobs` |
| `BACKEND_NO_LOCAL` | 400 | `/blobs` con Azure |
