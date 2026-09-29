/**
 * Núcleo del cliente de la API v1.1.
 *
 * Aquí vive todo lo transversal: sesión, transporte, manejo de errores,
 * paginación y el ciclo de vida de los archivos (subida por SAS en tres pasos,
 * polling de la revisión IA y descarga por URL firmada). Las funciones de cada
 * módulo del backend están en `lib/cliente.ts`, que se apoya en esto.
 *
 * Tres cosas que el backend rompió a propósito y que se absorben en este
 * archivo (ver `backend/RUPTURAS.md`):
 *
 * - **Errores unificados**: toda respuesta de error es `{error:{code,message}}`
 *   y se convierte en `ApiError`. Las pantallas muestran `err.message`, que es
 *   el texto que escribió el backend, no un genérico.
 * - **Listados envueltos**: `Pagina<T>`. `items()` y `paginarTodo()` evitan que
 *   un componente que solo quiere la lista tenga que saber de la envoltura.
 * - **`PATCH` sin campos devuelve 400 `SIN_CAMBIOS`**: el cliente ni lo emite.
 */
import type {
  ArchivoConfirmado, Conjunto, DownloadUrl, Pagina, Revision, UploadUrl,
  Usuario,
} from "./tipos";

/**
 * Origen del backend. El cliente siempre añade `/api/v1`.
 *
 * Si `NEXT_PUBLIC_API_URL` trae un sufijo `/api` o `/api/v1` (típico en Vercel),
 * se recorta para no acabar en `/api/api/v1/...`.
 */
function origenApi(): string {
  const raw = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").trim();
  return raw.replace(/\/+$/, "").replace(/\/api(?:\/v1)?$/i, "");
}
const BASE = origenApi();

/** Tope de `page_size` en el backend; por encima responde 422. */
export const PAGE_SIZE_MAX = 100;
/** Tamaño que aplica el backend cuando el cliente no pide `page_size`. */
export const PAGE_SIZE_DEFAULT = 25;

// ============================================================================
// Errores
// ============================================================================
/**
 * Error de la API con el `code` del backend, su `message` y el HTTP `status`.
 *
 * Es una clase y no un objeto plano para que `instanceof` funcione y para que
 * los `catch (e: any) { e.message }` que ya existían sigan mostrando el texto
 * correcto.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details?: unknown;

  constructor(code: string, message: string, status = 0, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export function esApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Texto para la interfaz: el `message` del backend si lo hay. */
export function mensajeError(e: unknown, porDefecto = "Error inesperado"): string {
  if (esApiError(e)) return e.message || porDefecto;
  if (e instanceof Error && e.message) return e.message;
  return porDefecto;
}

// ============================================================================
// Sesión
// ============================================================================
const CLAVE_TOKEN = "acredittia_token";
const CLAVE_REFRESH = "acredittia_refresh";
const CLAVE_USER = "acredittia_user";

function token(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(CLAVE_TOKEN);
}

export function refreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(CLAVE_REFRESH);
}

export function setSession(tok: string, refresh: string, user: unknown) {
  localStorage.setItem(CLAVE_TOKEN, tok);
  localStorage.setItem(CLAVE_REFRESH, refresh);
  localStorage.setItem(CLAVE_USER, JSON.stringify(user));
}

export function clearSession() {
  localStorage.removeItem(CLAVE_TOKEN);
  localStorage.removeItem(CLAVE_REFRESH);
  localStorage.removeItem(CLAVE_USER);
}

export function currentUser(): Usuario | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(CLAVE_USER);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Usuario;
  } catch {
    return null;
  }
}

/** Refresca la copia local del usuario (tras `PATCH /auth/me`, por ejemplo). */
export function guardarUsuario(user: Usuario) {
  if (typeof window === "undefined") return;
  localStorage.setItem(CLAVE_USER, JSON.stringify(user));
}

async function tryRefresh(): Promise<boolean> {
  const r = refreshToken();
  if (!r) return false;
  try {
    const res = await fetch(`${BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: r }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    localStorage.setItem(CLAVE_TOKEN, data.access_token);
    localStorage.setItem(CLAVE_REFRESH, data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

// ============================================================================
// Transporte
// ============================================================================
export type ValorQuery = string | number | boolean | null | undefined;
export type Query = Record<string, ValorQuery>;

/** Serializa la query descartando lo vacío (que el backend interpretaría). */
export function qs(params?: Query): string {
  if (!params) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export type Opciones = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Query;
};

/** Quita las claves `undefined` de un cuerpo de PATCH. */
function limpiar(body: unknown): Record<string, unknown> {
  const salida: Record<string, unknown> = {};
  if (!body || typeof body !== "object") return salida;
  for (const [k, v] of Object.entries(body as Record<string, unknown>)) {
    if (v !== undefined) salida[k] = v;
  }
  return salida;
}

function esRutaPublica(path: string): boolean {
  return path.startsWith("/auth/login") || path.startsWith("/auth/register")
    || path.startsWith("/auth/refresh") || path.startsWith("/auth/password/");
}

export async function api<T = any>(
  path: string,
  opts: Opciones = {},
  reintento = true,
): Promise<T> {
  const method = opts.method || (opts.body !== undefined ? "POST" : "GET");
  let body = opts.body;

  // §0.1: un PATCH sin campos responde 400 SIN_CAMBIOS. No se emite la petición.
  if (method === "PATCH") {
    const cambios = limpiar(body);
    if (Object.keys(cambios).length === 0) {
      throw new ApiError("SIN_CAMBIOS", "No hay cambios que guardar", 400);
    }
    body = cambios;
  }

  const headers: Record<string, string> = {};
  const t = token();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const companyId = typeof window !== "undefined"
    ? sessionStorage.getItem("impersonar") : null;
  if (companyId) headers["X-Company-Id"] = companyId;

  let res: Response;
  try {
    res = await fetch(`${BASE}/api/v1${path}${qs(opts.query)}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("SIN_CONEXION",
      "No se pudo contactar con el servidor. Revisa tu conexión.", 0);
  }

  if (res.status === 401 && !esRutaPublica(path)) {
    if (reintento && (await tryRefresh())) return api<T>(path, opts, false);
    clearSession();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new ApiError("SESION_EXPIRADA", "Tu sesión expiró. Vuelve a ingresar.",
      401);
  }

  const texto = await res.text();
  const data = texto ? safeJson(texto) : {};
  if (!res.ok) {
    const e = (data as any)?.error;
    throw new ApiError(e?.code ?? "ERROR",
      e?.message ?? `Error ${res.status}`, res.status, e?.details);
  }
  return data as T;
}

function safeJson(texto: string): unknown {
  try {
    return JSON.parse(texto);
  } catch {
    return {};
  }
}

/** Azúcar para los PATCH: no emite nada si no hay cambios reales. */
export function patch<T = any>(path: string, cambios: unknown): Promise<T> {
  return api<T>(path, { method: "PATCH", body: cambios });
}

// ============================================================================
// Paginación (§0 de RUPTURAS: todos los listados vienen envueltos)
// ============================================================================
export function paginaVacia<T>(page_size = PAGE_SIZE_DEFAULT): Pagina<T> {
  return { items: [], page: 1, page_size, total: 0, total_pages: 0 };
}

/**
 * Extrae `items` de cualquier envoltura (paginada o conjunto cerrado).
 *
 * Es el helper que permite que un componente que solo quiere la lista no tenga
 * que saber que existe la envoltura.
 */
export function items<T>(
  envoltura: Pagina<T> | Conjunto<T> | { items?: T[] } | null | undefined,
): T[] {
  return envoltura?.items ?? [];
}

/** Igual que `items()` pero sobre la promesa: `await lista(contratos.listar())`. */
export async function lista<T>(
  peticion: Promise<Pagina<T> | Conjunto<T> | { items?: T[] }>,
): Promise<T[]> {
  return items(await peticion);
}

/**
 * Recorre todas las páginas de un listado y devuelve la colección completa.
 *
 * Necesario para los selectores (faenas, contratos, cargos): sin `page_size` el
 * backend devuelve solo los primeros 25 elementos y el desplegable saldría
 * incompleto. `maxPaginas` es el freno para no encadenar peticiones sin fin.
 */
export async function paginarTodo<T>(
  pedir: (page: number, page_size: number) => Promise<Pagina<T>>,
  { pageSize = PAGE_SIZE_MAX, maxPaginas = 20 }: {
    pageSize?: number; maxPaginas?: number;
  } = {},
): Promise<T[]> {
  const acumulado: T[] = [];
  let page = 1;
  let totalPaginas = 1;
  do {
    const p = await pedir(page, pageSize);
    acumulado.push(...items(p));
    totalPaginas = p.total_pages ?? 1;
    page += 1;
  } while (page <= totalPaginas && page <= maxPaginas);
  return acumulado;
}

// ============================================================================
// §6.1 — Subida de documentos: SAS en tres pasos
// ============================================================================
export type EtapaSubida = "firmando" | "subiendo" | "confirmando" | "listo";
export type OnProgreso = (pct: number, etapa: EtapaSubida) => void;

/** Resultado de `subirDocumento`: la confirmación más el job de la IA. */
export type Subida = ArchivoConfirmado & {
  /** Job de la revisión IA para pasarlo a `esperarRevision`. */
  job_id: string;
};

/**
 * Sube un archivo a un documento con el flujo de tres pasos del backend.
 *
 * 1. `POST /documentos/{id}/upload-url` → SAS de escritura y `blob_path`.
 * 2. `PUT` del archivo crudo contra `upload_url` con las cabeceras que devuelve
 *    el paso 1. **Sin `Authorization`**: la URL ya está firmada y el header
 *    rompería la firma en Azure.
 * 3. `POST /documentos/{id}/archivos` con `{blob_path, filename}` → 201.
 *
 * La revisión IA queda `queued` y el documento sigue en `falta`: el veredicto se
 * obtiene con `esperarRevision(job_id)`.
 */
export async function subirDocumento(
  docId: string,
  file: File,
  onProgreso?: OnProgreso,
): Promise<Subida> {
  const avisar = (pct: number, etapa: EtapaSubida) => onProgreso?.(pct, etapa);

  avisar(2, "firmando");
  const sas = await api<UploadUrl>(`/documentos/${docId}/upload-url`, {
    body: {
      filename: file.name,
      content_type: file.type || undefined,
      size_bytes: file.size,
    },
  });

  if (sas.max_bytes && file.size > sas.max_bytes) {
    throw new ApiError("ARCHIVO_MUY_GRANDE",
      `El archivo supera el máximo de ${Math.round(sas.max_bytes / 1048576)} MB`,
      400);
  }

  avisar(5, "subiendo");
  await subirBlob(sas.upload_url, file, sas.headers, (pct) =>
    avisar(5 + Math.round(pct * 0.85), "subiendo"));

  avisar(92, "confirmando");
  const confirmado = await api<ArchivoConfirmado>(
    `/documentos/${docId}/archivos`,
    { body: { blob_path: sas.blob_path, filename: file.name } },
  );
  avisar(100, "listo");
  return { ...confirmado, job_id: confirmado.ia_review.job_id };
}

/**
 * `PUT` del archivo crudo contra la URL firmada.
 *
 * Se usa XMLHttpRequest y no `fetch` porque es la única forma de tener progreso
 * de subida en el navegador. Las cabeceras son exactamente las que emitió el
 * backend (en Azure incluyen `x-ms-blob-type`); no se añade ninguna más.
 */
function subirBlob(
  destino: string,
  file: File,
  headers: Record<string, string> | undefined,
  onProgreso?: (pct: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", destino, true);
    for (const [k, v] of Object.entries(headers ?? {})) {
      xhr.setRequestHeader(k, v);
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgreso?.(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onerror = () => reject(new ApiError("SUBIDA_FALLIDA",
      "No se pudo subir el archivo al almacenamiento", 0));
    xhr.ontimeout = () => reject(new ApiError("SUBIDA_FALLIDA",
      "La subida del archivo tardó demasiado", 0));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      const cuerpo = safeJson(xhr.responseText || "") as any;
      reject(new ApiError(cuerpo?.error?.code ?? "SUBIDA_FALLIDA",
        cuerpo?.error?.message
          ?? "El almacenamiento rechazó el archivo; vuelve a intentarlo",
        xhr.status));
    };
    xhr.send(file);
  });
}

// ============================================================================
// §6.1 — La revisión IA es asíncrona: polling
// ============================================================================
export type OpcionesEspera = {
  intervaloMs?: number;
  timeoutMs?: number;
  /** Se llama en cada sondeo, para pintar el estado «analizando». */
  onEstado?: (r: Revision) => void;
};

export function revisionTerminada(r: Revision | null | undefined): boolean {
  return r?.status === "done" || r?.status === "failed";
}

/**
 * Sondea `GET /ia/revisiones/{job_id}` hasta que el job termine.
 *
 * Al agotarse el tiempo lanza `ApiError('REVISION_TIMEOUT')` con la última
 * revisión conocida en `details`: el job sigue vivo en el servidor, así que la
 * interfaz debe decir «sigue analizando» y no «falló».
 */
export async function esperarRevision(
  jobId: string,
  { intervaloMs = 1500, timeoutMs = 60000, onEstado }: OpcionesEspera = {},
): Promise<Revision> {
  const limite = Date.now() + timeoutMs;
  let ultima: Revision | null = null;
  for (;;) {
    ultima = await api<Revision>(`/ia/revisiones/${jobId}`);
    onEstado?.(ultima);
    if (revisionTerminada(ultima)) return ultima;
    if (Date.now() + intervaloMs >= limite) {
      throw new ApiError("REVISION_TIMEOUT",
        "La revisión IA sigue en curso. Vuelve a consultarla en unos segundos.",
        408, ultima);
    }
    await esperar(intervaloMs);
  }
}

function esperar(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ============================================================================
// §6.2 — Descarga por URL firmada (el endpoint /descarga ya no existe)
// ============================================================================
export function urlDescarga(docId: string, archivoId: string): Promise<DownloadUrl> {
  return api<DownloadUrl>(
    `/documentos/${docId}/archivos/${archivoId}/download-url`);
}

/**
 * Abre una URL firmada de descarga.
 *
 * Se navega con un ancla temporal y no con `location.assign` para no abandonar
 * la SPA: el backend manda `Content-Disposition: attachment`, así que el
 * navegador guarda el archivo sin cambiar de página.
 */
export function abrirDescarga(url: string, filename?: string) {
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Pide la URL firmada del archivo y la abre. */
export async function descargarArchivo(docId: string, archivoId: string) {
  const dl = await urlDescarga(docId, archivoId);
  abrirDescarga(dl.download_url, dl.filename);
}
