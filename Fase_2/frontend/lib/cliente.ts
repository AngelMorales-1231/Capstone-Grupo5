/**
 * Funciones del cliente API, un objeto por módulo del backend (v1.1).
 *
 * Se importa entero para no colisionar con los nombres de las pantallas:
 *
 * ```ts
 * import * as Api from "@/lib/cliente";
 * const pagina = await Api.contratos.listar({ page_size: 100 });
 * const filas  = await Api.lista(Api.contratos.listar());   // solo los items
 * ```
 *
 * Convenciones:
 * - Todo listado devuelve `Pagina<T>`; los conjuntos cerrados, `Conjunto<T>`.
 * - Los `editar*` van por `patch()`, que no emite la petición si no hay cambios
 *   (el backend responde 400 `SIN_CAMBIOS`).
 * - Los módulos que el frontend todavía no cubre con pantallas (cargos,
 *   personas, calendario, reportes, integraciones, plataformas, credenciales)
 *   están igualmente aquí, con sus tipos, para que quien haga esas pantallas no
 *   tenga que escribir el cliente.
 */
import { api, patch, type Query } from "./api";
import type {
  AcreditacionesEstado, ActividadFila, AdminStats, Alerta, AlertaPatch,
  Ambito, ArchivoConfirmado, CategoriaEvento, ChecklistSujeto, Conjunto,
  Contrato, ContratoCreado, ContratoIn, ContratoPatch, Credencial,
  CumplimientoContrato, Documento, DocumentoFila, DownloadUrl, Ejemplo, Empresa,
  EmpresaDetalle, EquipoIn, EquipoPatch, EstadoAcceso, Eventos, Faena,
  FaenaAdmin, FaenaDetalle, Integracion, JobEncolado, Kpis, LicenciaInterna,
  Matriz, OlvidoPassword, Pagina, ParamsAlertas, ParamsDocumentos, ParamsEquipos,
  ParamsPagina, ParamsPersonal, PerfilActualizado, Persona, PlantillaAdmin,
  PlantillaRequisito, PlataformaContrato, PlataformaFaena, ProximoVencimiento,
  Proveedor, Cargo, CargoIn, CargoPatch, Reporte, ReporteProgramado,
  RequisitoFila, RequisitoTerreno, RequisitosCargo, ResetDemo, ResumenAlertas,
  Revision, RecursoExport, Sesion, Sujeto, SujetoCreado, SujetoDetalle,
  SyncLog, Tendencia, TipoReporte, Tokens, TrabajadorIn, TrabajadorPatch,
  Usuario, UsuarioEmpresa, Exportacion, Periodo,
} from "./tipos";

export * from "./api";
export type * from "./tipos";

/** Los `ParamsPagina` y los filtros viajan tal cual como query. */
const q = (p?: object): Query => (p ?? {}) as Query;

// ============================================================================
// §1 — auth
// ============================================================================
export const auth = {
  login: (email: string, password: string) =>
    api<Sesion>("/auth/login", { body: { email, password } }),

  register: (body: { empresa: string; rut: string; email: string; password: string }) =>
    api<{ ok: boolean; company_id: string; message?: string }>(
      "/auth/register", { body }),

  refresh: (refresh_token: string) =>
    api<Tokens>("/auth/refresh", { body: { refresh_token } }),

  logout: (refresh_token: string) =>
    api<{ ok: boolean }>("/auth/logout", { body: { refresh_token } }),

  me: () => api<Usuario>("/auth/me"),

  /** Cambiar `password` exige `password_actual`; `refresh_token` salva la sesión. */
  editarMe: (cambios: {
    nombre?: string; email?: string; password?: string;
    password_actual?: string; refresh_token?: string;
  }) => patch<PerfilActualizado>("/auth/me", cambios),

  /** Responde siempre 200, exista o no la cuenta. */
  olvidePassword: (email: string) =>
    api<OlvidoPassword>("/auth/password/forgot", { body: { email } }),

  /** Revoca todos los refresh tokens: después hay que volver a hacer login. */
  resetPassword: (token: string, password: string) =>
    api<{ ok: boolean; sesiones_cerradas: number; message: string }>(
      "/auth/password/reset", { body: { token, password } }),
};

// ============================================================================
// §2 — admin (back-office de la plataforma)
// ============================================================================
export const admin = {
  empresas: (params?: ParamsPagina & { status?: string; es_demo?: boolean }) =>
    api<Pagina<Empresa>>("/admin/companies", { query: q(params) }),

  empresa: (id: string) => api<EmpresaDetalle>(`/admin/companies/${id}`),

  aprobar: (id: string) =>
    api<Empresa>(`/admin/companies/${id}/approve`, { method: "POST", body: {} }),

  rechazar: (id: string, reason: string) =>
    api<Empresa>(`/admin/companies/${id}/reject`, { body: { reason } }),

  /** 409 `NO_ES_DEMO` si la empresa no lo es. La actividad NO se borra. */
  resetDemo: (id: string) =>
    api<ResetDemo>(`/admin/companies/${id}/reset-demo`, { method: "POST", body: {} }),

  stats: () => api<AdminStats>("/admin/stats"),

  crearFaena: (body: Partial<FaenaAdmin> & { nombre: string; mandante: string }) =>
    api<FaenaAdmin>("/admin/faenas", { body }),

  editarFaena: (id: string, cambios: Partial<FaenaAdmin>) =>
    patch<FaenaAdmin>(`/admin/faenas/${id}`, cambios),

  crearPlataformaFaena: (faenaId: string, body: {
    nombre: string; descripcion?: string | null; url?: string | null;
    nota?: string | null; orden?: number;
  }) => api<PlataformaFaena>(`/admin/faenas/${faenaId}/plataformas`, { body }),

  /** A diferencia de `/requisitos/templates`, incluye las inactivas. */
  plantillas: (params?: ParamsPagina & {
    ambito?: Ambito; faena_id?: string; activo?: boolean;
  }) => api<Pagina<PlantillaAdmin>>("/admin/requisitos/templates", { query: q(params) }),

  crearPlantilla: (body: Partial<PlantillaAdmin> & { ambito: Ambito; titulo: string }) =>
    api<PlantillaAdmin>("/admin/requisitos/templates", { body }),

  editarPlantilla: (id: string, cambios: Partial<PlantillaAdmin>) =>
    patch<PlantillaAdmin>(`/admin/requisitos/templates/${id}`, cambios),

  /** Borrado en blando (`activo=false`): no toca los documentos instanciados. */
  eliminarPlantilla: (id: string) =>
    api<PlantillaAdmin & { ok: boolean }>(
      `/admin/requisitos/templates/${id}`, { method: "DELETE" }),

  cargosBase: (params?: ParamsPagina & {
    categoria?: string; requiere_emsipor?: boolean; activo?: boolean;
  }) => api<Pagina<Cargo>>("/admin/cargos", { query: q(params) }),

  crearCargoBase: (body: CargoIn) => api<Cargo>("/admin/cargos", { body }),

  editarCargoBase: (id: string, cambios: CargoPatch) =>
    patch<Cargo>(`/admin/cargos/${id}`, cambios),

  eliminarCargoBase: (id: string) =>
    api<{ ok: boolean }>(`/admin/cargos/${id}`, { method: "DELETE" }),

  planes: (params?: ParamsPagina & { activo?: boolean }) =>
    api<Pagina<Record<string, unknown>>>("/admin/planes", { query: q(params) }),
};

// ============================================================================
// Empresa (autogestión) y sus usuarios
// ============================================================================
export const empresa = {
  perfil: () => api<Record<string, unknown>>("/company"),

  editar: (cambios: { nombre?: string; email?: string }) =>
    patch<Record<string, unknown>>("/company", cambios),

  usuarios: (params?: ParamsPagina & { role?: string; activo?: boolean }) =>
    api<Pagina<UsuarioEmpresa>>("/company/usuarios", { query: q(params) }),

  invitar: (body: {
    nombre: string; email: string; role?: "company" | "contract_admin";
    contrato_id?: string | null;
  }) => api<UsuarioEmpresa & { activacion?: Record<string, unknown> }>(
    "/company/usuarios", { body }),

  editarUsuario: (id: string, cambios: {
    nombre?: string; role?: string; contrato_id?: string | null; activo?: boolean;
  }) => patch<UsuarioEmpresa>(`/company/usuarios/${id}`, cambios),

  desactivarUsuario: (id: string) =>
    api<{ ok: boolean }>(`/company/usuarios/${id}`, { method: "DELETE" }),
};

// ============================================================================
// §3 — faenas y catálogos
// ============================================================================
export const faenas = {
  listar: (params?: ParamsPagina & {
    grupo?: string; region?: string; sector?: string; activa?: boolean;
    mandante?: string;
  }) => api<Pagina<Faena>>("/faenas", { query: q(params) }),

  detalle: (id: string) => api<FaenaDetalle>(`/faenas/${id}`),

  plataformas: (id: string, params?: ParamsPagina) =>
    api<Pagina<PlataformaFaena>>(`/faenas/${id}/plataformas`, { query: q(params) }),

  /** Upsert del acceso de la empresa. Requiere rol `company` o `admin`. */
  editarAcceso: (faenaId: string, plataformaId: string,
                 cambios: { estado: EstadoAcceso; nota?: string | null }) =>
    patch<PlataformaFaena>(
      `/faenas/${faenaId}/plataformas/${plataformaId}/acceso`, cambios),
};

export const catalogo = {
  /** Conjunto cerrado: `{items, total}`, no pagina. */
  tiposEquipo: () => api<Conjunto<string>>("/catalogo/tipos-equipo"),

  plantillas: (params?: ParamsPagina & { ambito?: Ambito; faena_id?: string }) =>
    api<Pagina<PlantillaRequisito>>("/catalogo/requisitos-templates",
      { query: q(params) }),

  /** El listado no trae `campos_clave` ni `notas`: hay que abrir el detalle. */
  ejemplos: (params?: ParamsPagina) =>
    api<Pagina<Ejemplo>>("/catalogo/ejemplos", { query: q(params) }),

  ejemplo: (clave: string) => api<Ejemplo>(`/catalogo/ejemplos/${clave}`),

  /** Conjunto cerrado: `{items, total}`, no pagina. */
  requisitosTerreno: (params?: { ambito?: "conductor" | "equipo"; nivel?: string }) =>
    api<Conjunto<RequisitoTerreno>>("/catalogo/requisitos-terreno",
      { query: q(params) }),

  laboratorios: (params?: ParamsPagina & { faena_id?: string }) =>
    api<Pagina<Proveedor>>("/catalogo/laboratorios", { query: q(params) }),

  talleres: (params?: ParamsPagina & { faena_id?: string }) =>
    api<Pagina<Proveedor>>("/catalogo/talleres", { query: q(params) }),

  proveedoresGps: (params?: ParamsPagina & { faena_id?: string }) =>
    api<Pagina<Proveedor>>("/catalogo/proveedores-gps", { query: q(params) }),
};

// ============================================================================
// Requisitos (catálogo con el estado real de la empresa)
// ============================================================================
export const requisitos = {
  listar: (params?: ParamsPagina & {
    ambito?: Ambito; tipo?: string; obligatorio?: boolean; estado?: string;
  }) => api<Pagina<RequisitoFila> & { kpis: Record<string, number> }>(
    "/requisitos", { query: q(params) }),

  /** Solo las plantillas activas. La clave del id es `template_id`. */
  plantillas: (params?: ParamsPagina & { ambito?: Ambito; faena_id?: string }) =>
    api<Pagina<PlantillaRequisito>>("/requisitos/templates", { query: q(params) }),
};

// ============================================================================
// §5 — cargos
// ============================================================================
export const cargos = {
  /** Cargos de la empresa más los globales de Acredittia. */
  listar: (params?: ParamsPagina & {
    categoria?: string; requiere_emsipor?: boolean; activo?: boolean;
  }) => api<Pagina<Cargo>>("/cargos", { query: q(params) }),

  crear: (body: CargoIn) => api<Cargo>("/cargos", { body }),

  /** `aplicar_retroactivo` propaga los requisitos nuevos a los trabajadores. */
  editar: (id: string, cambios: CargoPatch, aplicarRetroactivo = false) =>
    api<Cargo & { documentos_creados?: number; trabajadores_afectados?: number }>(
      `/cargos/${id}`, {
        method: "PATCH",
        body: cambios,
        query: aplicarRetroactivo ? { aplicar_retroactivo: true } : undefined,
      }),

  /** Borrado en blando: `activo=false`. */
  eliminar: (id: string) =>
    api<{ ok: boolean; id: string; activo: boolean }>(`/cargos/${id}`,
      { method: "DELETE" }),

  /** Plantilla efectiva: lo que se instanciará a un trabajador nuevo. */
  requisitos: (id: string) => api<RequisitosCargo>(`/cargos/${id}/requisitos`),
};

// ============================================================================
// §4 — contratos
// ============================================================================
export const contratos = {
  listar: (params?: ParamsPagina & { faena_id?: string; estado?: string }) =>
    api<Pagina<Contrato>>("/contratos", { query: q(params) }),

  detalle: (id: string) => api<Contrato>(`/contratos/${id}`),

  crear: (body: ContratoIn) => api<ContratoCreado>("/contratos", { body }),

  editar: (id: string, cambios: ContratoPatch) =>
    patch<Contrato>(`/contratos/${id}`, cambios),

  eliminar: (id: string) =>
    api<{ ok: boolean; sujetos_eliminados: number; archivos_eliminados: number }>(
      `/contratos/${id}`, { method: "DELETE", query: { confirm: true } }),

  documentos: (id: string, params?: ParamsPagina) =>
    api<Pagina<Documento>>(`/contratos/${id}/documentos`, { query: q(params) }),

  personal: (id: string, params?: Omit<ParamsPersonal, "contrato_id">) =>
    api<Pagina<Sujeto>>(`/contratos/${id}/personal`, { query: q(params) }),

  equipos: (id: string, params?: Omit<ParamsEquipos, "contrato_id">) =>
    api<Pagina<Sujeto>>(`/contratos/${id}/equipos`, { query: q(params) }),

  alertas: (id: string, params?: ParamsPagina & {
    severidad?: string; origen?: string; solo_activas?: boolean;
  }) => api<Pagina<Alerta>>(`/contratos/${id}/alertas`, { query: q(params) }),

  historial: (id: string, params?: ParamsPagina & { modulo?: string; tipo?: string }) =>
    api<Pagina<ActividadFila>>(`/contratos/${id}/historial`, { query: q(params) }),

  /**
   * Encola la extracción IA de un contrato ya subido. **No crea el contrato**:
   * el `job_id` se pasa después como `ia_review_id` en `crear()`.
   */
  analizar: (blob_path: string, filename: string) =>
    api<JobEncolado>("/contratos/analizar", { body: { blob_path, filename } }),

  /**
   * Matriz **dispersa** sujeto × requisito. Una celda ausente significa «el
   * requisito no aplica» y se pinta como hueco (`—`), no como incumplimiento.
   */
  matriz: (id: string, params?: {
    tipo?: "personal" | "equipo"; incluir_opcionales?: boolean;
    cargo_id?: string; page?: number; page_size?: number;
  }) => api<Matriz>(`/contratos/${id}/matriz`, { query: q(params) }),

  // --- requisitos personalizados del contrato ------------------------------
  vinculos: (id: string) =>
    api<Record<string, unknown>>(`/contratos/${id}/vinculos`),

  requisitos: (id: string, params?: ParamsPagina & {
    vinculo_tipo?: string; vinculo_ref?: string; ambito?: Ambito;
    cargo_id?: string; origen?: string;
  }) => api<Pagina<Record<string, unknown>>>(`/contratos/${id}/requisitos`,
    { query: q(params) }),

  crearRequisitos: (id: string, body: object | object[],
                    opciones?: { bulk?: boolean; aplicar_retroactivo?: boolean }) =>
    api<Record<string, unknown>>(`/contratos/${id}/requisitos`,
      { body, query: q(opciones) }),

  editarRequisito: (id: string, rid: string, cambios: object) =>
    patch<Record<string, unknown>>(`/contratos/${id}/requisitos/${rid}`, cambios),

  eliminarRequisito: (id: string, rid: string) =>
    api<{ ok: boolean }>(`/contratos/${id}/requisitos/${rid}`, { method: "DELETE" }),

  definirPlantilla: (id: string, ambito: Ambito, requisito_template_ids: string[]) =>
    api<Record<string, unknown>>(`/contratos/${id}/plantillas/${ambito}`,
      { method: "PUT", body: { requisito_template_ids } }),

  carpetaArranque: (id: string, blob_path: string, filename: string) =>
    api<JobEncolado>(`/contratos/${id}/carpeta-arranque`,
      { body: { blob_path, filename } }),
};

// ============================================================================
// Plataformas del contrato y credenciales
// ============================================================================
export const plataformas = {
  /** Conjunto cerrado; `heredado` indica que aún son las de la faena. */
  listar: (contratoId: string) =>
    api<Conjunto<PlataformaContrato> & { heredado: boolean }>(
      `/contratos/${contratoId}/plataformas`),

  crear: (contratoId: string, body: {
    nombre: string; descripcion?: string | null; url?: string | null;
    color?: string | null; nota?: string | null;
  }) => api<PlataformaContrato>(`/contratos/${contratoId}/plataformas`, { body }),

  editar: (contratoId: string, pid: string, cambios: {
    nombre?: string; descripcion?: string | null; url?: string | null;
    color?: string | null; nota?: string | null; orden?: number;
    estado?: EstadoAcceso;
  }) => patch<PlataformaContrato>(
    `/contratos/${contratoId}/plataformas/${pid}`, cambios),

  eliminar: (contratoId: string, pid: string) =>
    api<{ ok: boolean; requisitos_eliminados: number; credenciales_eliminadas: number }>(
      `/contratos/${contratoId}/plataformas/${pid}`, { method: "DELETE" }),

  solicitarAcceso: (contratoId: string, pid: string, nota?: string) =>
    api<PlataformaContrato>(
      `/contratos/${contratoId}/plataformas/${pid}/solicitar-acceso`,
      { body: { nota: nota ?? null } }),
};

/** Cuentas de acceso a una plataforma. El secreto nunca vuelve del backend. */
export const credenciales = {
  listar: (contratoId: string, pid: string, params?: ParamsPagina & { estado?: string }) =>
    api<Pagina<Credencial>>(
      `/contratos/${contratoId}/plataformas/${pid}/usuarios`, { query: q(params) }),

  crear: (contratoId: string, pid: string,
          body: { nombre: string; usuario: string; password: string }) =>
    api<Credencial>(`/contratos/${contratoId}/plataformas/${pid}/usuarios`, { body }),

  editar: (contratoId: string, pid: string, uid: string, cambios: {
    nombre?: string; usuario?: string; password?: string; estado?: "revocada";
  }) => patch<Credencial>(
    `/contratos/${contratoId}/plataformas/${pid}/usuarios/${uid}`, cambios),

  rotar: (contratoId: string, pid: string, uid: string, password: string) =>
    api<Credencial>(
      `/contratos/${contratoId}/plataformas/${pid}/usuarios/${uid}/rotar`,
      { body: { password } }),

  eliminar: (contratoId: string, pid: string, uid: string) =>
    api<{ ok: boolean; versiones_eliminadas: number }>(
      `/contratos/${contratoId}/plataformas/${pid}/usuarios/${uid}`,
      { method: "DELETE" }),

  usos: (contratoId: string, pid: string, uid: string, limite = 100) =>
    api<Record<string, unknown>>(
      `/contratos/${contratoId}/plataformas/${pid}/usuarios/${uid}/usos`,
      { query: { limite } }),
};

// ============================================================================
// §5 — personal y equipos
// ============================================================================
export const personal = {
  listar: (params?: ParamsPersonal) =>
    api<Pagina<Sujeto>>("/personal", { query: q(params) }),

  detalle: (id: string) => api<SujetoDetalle>(`/personal/${id}`),

  /** Acepta `cargo_id` o `cargo` como texto; el texto puede crear el cargo. */
  crear: (body: TrabajadorIn) => api<SujetoCreado>("/personal", { body }),

  /** Solo campos de personal: los de equipo se ignorarían en silencio. */
  editar: (id: string, cambios: TrabajadorPatch) =>
    patch<SujetoDetalle & { cargo_creado?: boolean; expediente_emsipor_creado?: boolean }>(
      `/personal/${id}`, cambios),

  /** Checklist completo del trabajador (conjunto cerrado, no pagina). */
  documentos: (id: string) => api<ChecklistSujeto>(`/personal/${id}/documentos`),

  baja: (id: string) =>
    api<{ ok: boolean; id: string; estado: "baja" }>(`/personal/${id}/baja`,
      { method: "POST", body: {} }),

  eliminar: (id: string) =>
    api<{ ok: boolean; archivos_eliminados: number }>(`/personal/${id}`,
      { method: "DELETE" }),
};

export const equipos = {
  listar: (params?: ParamsEquipos) =>
    api<Pagina<Sujeto>>("/equipos", { query: q(params) }),

  detalle: (id: string) => api<SujetoDetalle>(`/equipos/${id}`),

  crear: (body: EquipoIn) => api<SujetoCreado>("/equipos", { body }),

  /** Solo campos de equipo: los de personal se ignorarían en silencio. */
  editar: (id: string, cambios: EquipoPatch) =>
    patch<SujetoDetalle>(`/equipos/${id}`, cambios),

  documentos: (id: string) => api<ChecklistSujeto>(`/equipos/${id}/documentos`),

  baja: (id: string) =>
    api<{ ok: boolean; id: string; estado: "baja" }>(`/equipos/${id}/baja`,
      { method: "POST", body: {} }),

  eliminar: (id: string) =>
    api<{ ok: boolean; archivos_eliminados: number }>(`/equipos/${id}`,
      { method: "DELETE" }),
};

/** Licencia interna de mina. 409 `NO_REQUIERE_EMSIPOR` si el cargo no la exige. */
export const licencia = {
  detalle: (sujetoId: string) =>
    api<LicenciaInterna>(`/personal/${sujetoId}/licencia-interna`),

  /** `estado` no se acepta: lo deriva el backend de `numero` y `vence`. */
  editar: (sujetoId: string, cambios: { numero?: string | null; vence?: string | null }) =>
    patch<LicenciaInterna>(`/personal/${sujetoId}/licencia-interna`, cambios),

  /** Irreversible: borra los documentos EMSIPOR con sus archivos y blobs. */
  reset: (sujetoId: string) =>
    api<LicenciaInterna>(`/personal/${sujetoId}/licencia-interna/reset`,
      { method: "POST", body: {} }),
};

// ============================================================================
// Personas y flota (vistas consolidadas por RUT / patente)
// ============================================================================
export const personas = {
  listar: (params?: ParamsPagina & {
    cargo_id?: string; estado?: string; faena_id?: string; sin_asignacion?: boolean;
  }) => api<Pagina<Persona>>("/personas", { query: q(params) }),

  ficha: (rut: string) => api<Persona>(`/personas/${encodeURIComponent(rut)}`),
};

export const flota = {
  listar: (params?: ParamsPagina & {
    tipo_equipo?: string; estado?: string; faena_id?: string;
  }) => api<Pagina<Persona>>("/flota", { query: q(params) }),

  ficha: (patente: string) => api<Persona>(`/flota/${encodeURIComponent(patente)}`),
};

// ============================================================================
// §6 — documentos
// ============================================================================
export const documentos = {
  /** Listado transversal con el dueño resuelto y el conteo de archivos. */
  listar: (params?: ParamsDocumentos) =>
    api<Pagina<DocumentoFila>>("/documentos", { query: q(params) }),

  detalle: (id: string) => api<Documento>(`/documentos/${id}`),

  /** Al fijar `ok` sin `vence` el backend lo deriva (`vence_derivado`). */
  editar: (id: string, cambios: { estado?: "ok" | "falta"; vence?: string | null }) =>
    patch<Documento>(`/documentos/${id}`, cambios),

  /** Paso 1 de la subida. Normalmente se usa `subirDocumento()`. */
  urlSubida: (id: string, body: {
    filename: string; content_type?: string; size_bytes?: number;
  }) => api<Record<string, unknown>>(`/documentos/${id}/upload-url`, { body }),

  /** Paso 3 de la subida. Normalmente se usa `subirDocumento()`. */
  confirmarArchivo: (id: string, blob_path: string, filename: string) =>
    api<ArchivoConfirmado>(`/documentos/${id}/archivos`,
      { body: { blob_path, filename } }),

  urlDescarga: (id: string, archivoId: string) =>
    api<DownloadUrl>(`/documentos/${id}/archivos/${archivoId}/download-url`),

  eliminarArchivo: (id: string, archivoId: string) =>
    api<{ ok: boolean; documento: Documento }>(
      `/documentos/${id}/archivos/${archivoId}`, { method: "DELETE" }),
};

// ============================================================================
// Revisión y extracción IA (todo asíncrono: se consulta con polling)
// ============================================================================
export const ia = {
  /** Vuelve a revisar un archivo ya subido. */
  revisar: (archivo_id: string) =>
    api<JobEncolado>("/ia/revisiones", { body: { archivo_id } }),

  revision: (jobId: string) => api<Revision>(`/ia/revisiones/${jobId}`),

  revisiones: (params?: ParamsPagina & {
    documento_id?: string; sujeto_id?: string; resultado?: string;
    context?: string; status?: string;
  }) => api<Pagina<Revision>>("/ia/revisiones", { query: q(params) }),

  extraerSujeto: (blob_path: string, filename: string, tipo: "cedula" | "padron") =>
    api<JobEncolado>("/ia/extraer-sujeto", { body: { blob_path, filename, tipo } }),

  extraerContrato: (blob_path: string, filename: string) =>
    api<JobEncolado>("/ia/extraer-contrato", { body: { blob_path, filename } }),

  extraerCarpetaArranque: (blob_path: string, filename: string, contrato_id: string) =>
    api<JobEncolado>("/ia/extraer-carpeta-arranque",
      { body: { blob_path, filename, contrato_id } }),
};

// ============================================================================
// §7 — alertas
// ============================================================================
export const alertas = {
  listar: (params?: ParamsAlertas) =>
    api<Pagina<Alerta>>("/alertas", { query: q(params) }),

  resumen: () => api<ResumenAlertas>("/alertas/resumen"),

  /** `estado='resuelta'` fija `resuelta_at`; cualquier otro estado la limpia. */
  editar: (id: string, cambios: AlertaPatch) =>
    patch<Alerta>(`/alertas/${id}`, cambios),

  marcarLeidas: (ids?: string[]) =>
    api<{ marcadas: number }>("/alertas/marcar-leidas",
      { body: { ids: ids ?? null } }),
};

// ============================================================================
// §8 — dashboard y actividad
// ============================================================================
export const dashboard = {
  kpis: () => api<Kpis>("/dashboard/kpis"),

  cumplimientoContratos: (params?: ParamsPagina) =>
    api<Pagina<CumplimientoContrato>>("/dashboard/cumplimiento-contratos",
      { query: q(params) }),

  acreditacionesEstado: () =>
    api<AcreditacionesEstado>("/dashboard/acreditaciones-estado"),

  /** `limit` ya no existe: se pagina con `page` / `page_size`. */
  actividad: (params?: ParamsPagina) =>
    api<Pagina<ActividadFila>>("/dashboard/actividad", { query: q(params) }),

  proximosVencimientos: (params?: ParamsPagina & { dias?: number }) =>
    api<Pagina<ProximoVencimiento>>("/dashboard/proximos-vencimientos",
      { query: q(params) }),

  /** `anterior` y `delta_pct` pueden NO venir: hay que comprobar las claves. */
  tendencia: (params?: {
    periodo?: Periodo; desde?: string; hasta?: string; contrato_id?: string;
    faena_id?: string;
  }) => api<Tendencia>("/dashboard/tendencia", { query: q(params) }),
};

export const actividad = {
  listar: (params?: ParamsPagina & {
    modulo?: string; tipo?: string; user_id?: string; contrato_id?: string;
    desde?: string; hasta?: string;
  }) => api<Pagina<ActividadFila>>("/actividad", { query: q(params) }),
};

// ============================================================================
// Calendario
// ============================================================================
export const calendario = {
  /** `desde` y `hasta` son obligatorios; el rango máximo es de 366 días. */
  eventos: (desde: string, hasta: string, categoria?: CategoriaEvento) =>
    api<Eventos>("/calendario/eventos", { query: { desde, hasta, categoria } }),

  crear: (body: {
    titulo: string; fecha: string; categoria?: Exclude<CategoriaEvento, "vencimiento">;
    descripcion?: string | null;
  }) => api<Eventos["items"][number]>("/calendario/eventos", { body }),

  editar: (id: string, cambios: {
    titulo?: string; fecha?: string;
    categoria?: Exclude<CategoriaEvento, "vencimiento">;
    descripcion?: string | null; completado?: boolean;
  }) => patch<Eventos["items"][number]>(`/calendario/eventos/${id}`, cambios),

  eliminar: (id: string) =>
    api<{ ok: boolean; id: string; titulo: string }>(`/calendario/eventos/${id}`,
      { method: "DELETE" }),
};

// ============================================================================
// Reportes y exportaciones
// ============================================================================
export const reportes = {
  listar: (params?: ParamsPagina & {
    tipo?: TipoReporte; formato?: string; status?: string; desde?: string;
    hasta?: string;
  }) => api<Pagina<Reporte>>("/reportes", { query: q(params) }),

  /** Siempre asíncrono: se consulta con `detalle()` y se baja con `urlDescarga()`. */
  crear: (body: {
    tipo: TipoReporte; formato: "pdf" | "excel";
    params?: Record<string, unknown>; nombre?: string;
  }) => api<{ id: string; status: string }>("/reportes", { body }),

  detalle: (id: string) => api<Reporte>(`/reportes/${id}`),

  /** 409 `REPORTE_NO_LISTO` mientras el job no haya terminado. */
  urlDescarga: (id: string) => api<DownloadUrl>(`/reportes/${id}/download-url`),

  programados: (params?: ParamsPagina & { activo?: boolean; tipo?: TipoReporte }) =>
    api<Pagina<ReporteProgramado>>("/reportes/programados", { query: q(params) }),

  crearProgramado: (body: {
    nombre: string; tipo: TipoReporte; formato: "pdf" | "excel";
    cron_expr: string; params?: Record<string, unknown>; activo?: boolean;
  }) => api<ReporteProgramado>("/reportes/programados", { body }),

  editarProgramado: (id: string, cambios: Partial<{
    nombre: string; tipo: TipoReporte; formato: "pdf" | "excel";
    params: Record<string, unknown>; cron_expr: string; activo: boolean;
  }>) => patch<ReporteProgramado>(`/reportes/programados/${id}`, cambios),

  eliminarProgramado: (id: string) =>
    api<{ ok: boolean }>(`/reportes/programados/${id}`, { method: "DELETE" }),
};

export const exportaciones = {
  /**
   * Exporta una vista. Hasta `EXPORT_FILAS_MAX` responde 200 con `download_url`;
   * por encima devuelve 202 con el `id` del reporte que hay que sondear.
   */
  crear: (body: {
    recurso: RecursoExport; filtros?: Record<string, unknown>;
    formato?: "excel" | "csv";
  }) => api<Exportacion>("/exportaciones", { body }),
};

// ============================================================================
// Integraciones
// ============================================================================
export const integraciones = {
  listar: (params?: ParamsPagina & { tipo?: string; estado?: string }) =>
    api<Pagina<Integracion> & {
      kpis: Record<string, number>;
      tipos_disponibles: { tipo: string; nombre: string }[];
    }>("/integraciones", { query: q(params) }),

  /** El secreto no se guarda en la base: solo su referencia en el vault. */
  crear: (body: {
    tipo: string; credenciales?: Record<string, unknown>;
    config?: Record<string, unknown>;
  }) => api<Integracion>("/integraciones", { body }),

  editar: (id: string, cambios: {
    estado?: "activa" | "desconectada"; config?: Record<string, unknown>;
    credenciales?: Record<string, unknown>;
  }) => patch<Integracion>(`/integraciones/${id}`, cambios),

  eliminar: (id: string) =>
    api<{ ok: boolean; logs_eliminados: number }>(`/integraciones/${id}`,
      { method: "DELETE" }),

  /** 409 si está `desconectada`. El resultado real aparece en `logs()`. */
  sincronizar: (id: string) =>
    api<{ job_id: string; status: string; integracion_id: string; corrida: string }>(
      `/integraciones/${id}/sync`, { method: "POST", body: {} }),

  logs: (id: string, params?: ParamsPagina & {
    status?: "exito" | "error"; desde?: string; hasta?: string;
  }) => api<Pagina<SyncLog> & { integracion: Integracion }>(
    `/integraciones/${id}/logs`, { query: q(params) }),
};

// ============================================================================
// Notificaciones y suscripción
// ============================================================================
export const notificaciones = {
  preferencias: () => api<Record<string, unknown>>("/notificaciones/preferencias"),

  editarPreferencias: (preferencias: {
    evento: string; canal_email?: boolean; canal_whatsapp?: boolean;
    user_id?: string | null;
  }[]) => patch<Record<string, unknown>>("/notificaciones/preferencias",
    { preferencias }),
};

export const suscripcion = {
  planes: () => api<Pagina<Record<string, unknown>>>("/planes"),

  detalle: () => api<Record<string, unknown>>("/suscripcion"),

  contratar: (plan_id: string) =>
    api<Record<string, unknown>>("/suscripcion", { body: { plan_id } }),

  cancelar: () => api<{ ok: boolean }>("/suscripcion", { method: "DELETE" }),

  facturas: (params?: ParamsPagina & {
    estado?: string; desde?: string; hasta?: string;
  }) => api<Pagina<Record<string, unknown>>>("/facturas", { query: q(params) }),

  urlFactura: (id: string) => api<DownloadUrl>(`/facturas/${id}/download-url`),
};
