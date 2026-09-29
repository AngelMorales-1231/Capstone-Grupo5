/**
 * Tipos del contrato de la API v1.1 (ver `backend/RUPTURAS.md`).
 *
 * Dos reglas transversales que conviene tener presentes al leer este archivo:
 *
 * 1. **Todos los listados llegan envueltos** en `Pagina<T>`. Los conjuntos
 *    cerrados que el backend decidió no paginar (tipos de equipo, requisitos de
 *    terreno, checklist de un sujeto, plataformas de un contrato) usan
 *    `Conjunto<T>`, que solo trae `items` y `total`.
 * 2. **Los errores siempre son `{error: {code, message, details?}}`**. El
 *    cliente los convierte en `ApiError` (ver `lib/api.ts`), así que en las
 *    pantallas se muestra `err.message`, que es el texto del backend.
 */

// ============================================================================
// Envoltorios
// ============================================================================
export type Pagina<T> = {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
};

/** Listados que el backend NO pagina a propósito (conjuntos cerrados). */
export type Conjunto<T> = { items: T[]; total: number };

/** Parámetros comunes de todo listado paginado. */
export type ParamsPagina = {
  page?: number;
  /** Tope del backend: `PAGE_SIZE_MAX` = 100 (más devuelve 422). */
  page_size?: number;
  /** `campo` o `-campo` para descendente. */
  sort?: string;
  search?: string;
};

// ============================================================================
// Vocabularios cerrados
// ============================================================================
export type Rol = "admin" | "company" | "contract_admin";
export type EstadoEmpresa = "pending" | "approved" | "rejected";
export type EstadoDoc = "ok" | "falta";
export type EstadoCalc = "ok" | "porvenc" | "venc" | "falta";
export type EstadoSujeto = "ok" | "proc" | "venc" | "falta" | "baja";
export type EstadoContrato = "vigente" | "en_evaluacion" | "terminado";
export type TipoSujeto = "trabajador" | "equipo";
export type Ambito = "empresa" | "personal" | "equipo" | "emsipor";
export type TipoRequisito =
  | "legal" | "medico" | "capacitacion" | "certificacion" | "tecnico"
  | "medioambiental";
export type Severidad =
  | "critica" | "alta" | "media" | "baja" | "advertencia" | "informativa";
export type OrigenAlerta = "vencimiento" | "ia" | "integracion" | "sistema";
export type EstadoAlerta =
  | "nueva" | "en_progreso" | "bloqueante" | "informativa" | "resuelta";
export type EstadoAcceso = "activa" | "solicitada" | "sin_acceso";
export type EstadoJob = "queued" | "processing" | "done" | "failed";
export type ResultadoIa = "validado" | "con_observaciones" | "con_errores";
export type ContextoIa =
  | "empresa" | "personal" | "equipo" | "contrato" | "emsipor" | "cedula"
  | "padron" | "carpeta_arranque";
export type CategoriaCargo =
  | "conduccion" | "operacion" | "supervision" | "mantencion"
  | "administracion" | "otro";
export type CategoriaEvento =
  | "vencimiento" | "mantencion" | "capacitacion" | "administrativo"
  | "entrega" | "otro";
export type TipoActividad =
  | "creacion" | "actualizacion" | "subida_documento" | "asignacion"
  | "alerta_ia" | "visualizacion" | "comentario";
export type EstadoLicencia = "pendiente" | "por_vencer" | "vigente";
export type EstadoEmsipor = "pendiente" | "parcial" | "aprobado";
export type TipoIntegracion =
  | "siga" | "workmate" | "metacontratas" | "webcontrol" | "whatsapp"
  | "gdrive";
export type EstadoIntegracion = "activa" | "con_error" | "desconectada";
export type Periodo = "semana" | "mes" | "trimestre";
export type TipoReporte =
  | "estado_acreditacion" | "cumplimiento_requisitos" | "personal_acreditado"
  | "equipos_vehiculos" | "vencimientos" | "matriz_cumplimiento";
export type RecursoExport =
  | "personal" | "equipos" | "personas" | "flota" | "documentos"
  | "requisitos" | "alertas" | "contratos" | "matriz";

// ============================================================================
// Autenticación y usuario
// ============================================================================
export type EmpresaResumen = { id: string; nombre: string; rut: string };

export type ContratoResumen = {
  id: string;
  nombre: string;
  codigo: string | null;
};

/** Forma ampliada de `GET /auth/me` y de `login.user` (§1 de RUPTURAS). */
export type Usuario = {
  id: string;
  email: string;
  role: Rol;
  nombre: string | null;
  activo: boolean;
  contrato_id: string | null;
  /** Solo viene poblado para `role = contract_admin`. */
  contrato: ContratoResumen | null;
  last_login_at: string | null;
  company: EmpresaResumen | null;
};

export type Sesion = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user: Usuario;
};

export type Tokens = {
  access_token: string;
  refresh_token: string;
  token_type: string;
};

export type PerfilActualizado = Usuario & {
  cambios: string[];
  sesiones_cerradas: number;
};

/** En producción `_dev_token` NO aparece: no se puede depender de él. */
export type OlvidoPassword = {
  ok: boolean;
  message: string;
  _dev_token?: string;
  _dev_nota?: string;
};

// ============================================================================
// Documentos y archivos
// ============================================================================
export type Archivo = {
  id: string;
  filename: string;
  size_bytes: number | null;
  content_type: string | null;
  created_at: string | null;
  ia_review_id?: string | null;
  blob_path?: string;
};

export type Documento = {
  id: string;
  titulo: string;
  obligatorio: boolean;
  estado: EstadoDoc;
  estado_calc: EstadoCalc;
  vence: string | null;
  dias_para_vencer: number | null;
  es_emsipor: boolean;
  template_id: string | null;
  ejemplo_clave: string | null;
  plataforma: string | null;
  archivos_count: number;
  /** Solo en el detalle y en los checklist; en los listados llega el conteo. */
  archivos?: Archivo[];
  /** Solo al hacer PATCH: el vencimiento se derivó de la plantilla. */
  vence_derivado?: boolean;
  nota?: string;
};

export type DuenoDocumento = {
  tipo: "trabajador" | "equipo" | "contrato";
  id: string;
  nombre: string;
  rut?: string | null;
  patente?: string | null;
  codigo?: string | null;
};

/** Fila de `GET /documentos` (listado transversal, §6.3). */
export type DocumentoFila = Documento & {
  dueno: DuenoDocumento | null;
  contrato_id: string | null;
};

export type UploadUrl = {
  upload_url: string;
  blob_path: string;
  expires_at: string;
  /** Cabeceras del PUT. **Nunca** se les añade Authorization. */
  headers: Record<string, string>;
  method: "PUT";
  max_bytes: number;
  confirmar_en: string;
};

export type DownloadUrl = {
  download_url: string;
  expires_at: string;
  filename: string;
  content_type?: string | null;
  size_bytes?: number | null;
};

/** Respuesta de `POST /documentos/{id}/archivos` (la IA queda en `queued`). */
export type ArchivoConfirmado = {
  archivo: Archivo;
  ia_review: { job_id: string; status: EstadoJob; context: ContextoIa };
  documento: Documento;
  nota?: string;
};

export type ParamsDocumentos = ParamsPagina & {
  sujeto_id?: string;
  contrato_id?: string;
  estado?: EstadoDoc;
  estado_calc?: EstadoCalc;
  obligatorio?: boolean;
  es_emsipor?: boolean;
  vence_antes?: string;
  vence_despues?: string;
};

// ============================================================================
// Revisión IA
// ============================================================================
export type Hallazgo = {
  tipo: "error" | "warning" | "info";
  codigo: string;
  mensaje: string;
  campo: string | null;
  valor_detectado: string | null;
  valor_esperado: string | null;
};

export type Revision = {
  job_id: string;
  status: EstadoJob;
  context: ContextoIa;
  resultado: ResultadoIa | null;
  confianza: number | null;
  campos_extraidos: Record<string, unknown>;
  hallazgos: Hallazgo[];
  archivo_id: string | null;
  documento_id: string | null;
  contrato_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  accion_aplicada: string | null;
  created_at: string | null;
};

export type JobEncolado = {
  job_id: string;
  status: EstadoJob;
  context: ContextoIa;
  nota?: string;
  contrato_id?: string;
};

// ============================================================================
// Faenas y catálogos
// ============================================================================
export type StatsFaena = {
  contratos: number;
  personal: number;
  equipos: number;
  personal_acreditado: number;
  equipos_acreditados: number;
  docs_total: number;
  docs_ok: number;
  cumplimiento_pct: number;
};

export type Faena = {
  id: string;
  nombre: string;
  mandante: string;
  grupo: string | null;
  region: string | null;
  sector: string;
  activa: boolean;
  logo_url: string | null;
  color: string | null;
  lat: number | null;
  lng: number | null;
  stats: StatsFaena;
};

export type Acceso = {
  estado: EstadoAcceso;
  nota: string | null;
  solicitado_at: string | null;
  habilitado_at: string | null;
};

export type PlataformaFaena = {
  id: string;
  faena_id: string;
  nombre: string;
  descripcion: string | null;
  url: string | null;
  nota: string | null;
  orden: number;
  acceso: Acceso;
  /** Solo en la respuesta del PATCH de acceso. */
  creado?: boolean;
  estado_anterior?: EstadoAcceso;
};

export type FaenaDetalle = Faena & {
  plataformas: PlataformaFaena[];
  requisitos_faena: number;
};

export type Ejemplo = {
  clave: string;
  nombre: string;
  referencia: string | null;
  tip: string | null;
  pdf_url: string | null;
  tiene_campos_clave: boolean;
  tiene_notas: boolean;
  /** Solo en `GET /catalogo/ejemplos/{clave}`. */
  campos_clave?: [string, string][];
  notas?: string[];
};

export type PlantillaRequisito = {
  id: string;
  ambito: Ambito;
  titulo: string;
  codigo: string | null;
  tipo: TipoRequisito | null;
  obligatorio: boolean;
  vigencia_meses: number | null;
  plataforma: string | null;
  ejemplo_clave: string | null;
  faena_id: string | null;
};

export type RequisitoTerreno = {
  id: string;
  ambito: "conductor" | "equipo";
  titulo: string;
  descripcion: string | null;
  nivel: string | null;
  icono: string | null;
  referencia: string | null;
};

export type Proveedor = {
  id: string;
  categoria: string;
  nombre: string;
  localidad: string | null;
  certificacion: string | null;
  faena_id: string | null;
};

// ============================================================================
// Contratos
// ============================================================================
export type StatsContrato = {
  cumplimiento_pct: number;
  personal: { total: number; acreditados: number };
  equipos: { total: number; acreditados: number };
  docs_empresa: { total: number; ok: number };
  alertas_activas: number;
};

export type Contrato = {
  id: string;
  nombre: string;
  codigo: string | null;
  estado: EstadoContrato;
  fecha_inicio: string | null;
  fecha_termino: string | null;
  renovacion_automatica: boolean;
  origen_ia_review_id: string | null;
  faena: {
    id: string;
    nombre: string;
    mandante: string;
    grupo: string | null;
    region: string | null;
    color: string | null;
  };
  stats: StatsContrato;
};

export type ContratoCreado = Contrato & { documentos_creados: number };

export type ContratoIn = {
  nombre: string;
  codigo?: string | null;
  faena_id: string;
  fecha_inicio?: string | null;
  fecha_termino?: string | null;
  renovacion_automatica?: boolean;
  /** Job de `POST /contratos/analizar` que originó el alta. */
  ia_review_id?: string | null;
};

export type ContratoPatch = {
  nombre?: string;
  codigo?: string | null;
  fecha_inicio?: string | null;
  fecha_termino?: string | null;
  renovacion_automatica?: boolean;
  estado?: EstadoContrato;
};

export type CeldaMatriz = {
  col: number;
  estado_calc: EstadoCalc;
  vence: string | null;
};

export type FilaMatriz = {
  sujeto_id: string;
  nombre: string;
  rut: string | null;
  cargo: string | null;
  cumplimiento_pct: number;
  /** Matriz **dispersa**: una celda ausente es «no aplica», no `falta`. */
  celdas: CeldaMatriz[];
};

export type Matriz = {
  tipo: "personal" | "equipo";
  contrato_id: string;
  incluir_opcionales: boolean;
  columnas: { titulo: string; obligatorio: boolean; ambito: Ambito }[];
  filas: FilaMatriz[];
  page: number;
  page_size: number;
  total_filas: number;
  total_pages: number;
};

// ============================================================================
// Sujetos: personal y equipos
// ============================================================================
export type StatsSujeto = {
  total: number;
  ok: number;
  cumplimiento_pct: number;
};

export type Sujeto = {
  id: string;
  tipo: TipoSujeto;
  estado: EstadoSujeto;
  nombre: string;
  rut: string | null;
  cargo: string | null;
  cargo_id: string | null;
  es_conductor: boolean;
  patente: string | null;
  tipo_equipo: string | null;
  marca: string | null;
  modelo: string | null;
  anio: number | null;
  contrato: { id: string; nombre: string; faena_id: string; faena: string };
  stats: StatsSujeto;
};

export type LicenciaResumen = {
  numero: string | null;
  estado: EstadoLicencia;
  vence: string | null;
  emsipor_estado: EstadoEmsipor;
};

export type SujetoDetalle = Sujeto & {
  documentos: Documento[];
  documentos_emsipor: Documento[];
  licencia_interna: LicenciaResumen | null;
};

export type SujetoCreado = Sujeto & {
  documentos_creados: number;
  expediente_emsipor_creado?: boolean;
  /** El backend creó el cargo a partir del texto libre: hay que avisarlo. */
  cargo_creado?: boolean;
};

export type ChecklistSujeto = {
  sujeto: {
    id: string;
    tipo: TipoSujeto;
    nombre: string;
    rut: string | null;
    patente: string | null;
    estado: EstadoSujeto;
    cargo: string | null;
    contrato_id: string;
  };
  items: Documento[];
  documentos_emsipor: Documento[];
  total: number;
  stats: StatsSujeto;
};

export type TrabajadorIn = {
  contrato_id: string;
  nombre: string;
  rut: string;
  /** Recomendado. 400 `CARGO_INEXISTENTE` si no es de la empresa. */
  cargo_id?: string | null;
  /** Texto libre: el backend resuelve o crea el cargo (`cargo_creado`). */
  cargo?: string | null;
  es_conductor?: boolean;
};

/** `PATCH /personal/{id}`: solo estos campos (§5 de RUPTURAS). */
export type TrabajadorPatch = {
  nombre?: string;
  cargo_id?: string | null;
  cargo?: string | null;
  es_conductor?: boolean;
};

export type EquipoIn = {
  contrato_id: string;
  patente: string;
  tipo_equipo: string;
  marca?: string | null;
  modelo?: string | null;
  anio?: number | null;
};

/** `PATCH /equipos/{id}`: solo estos campos (§5 de RUPTURAS). */
export type EquipoPatch = {
  nombre?: string;
  marca?: string | null;
  modelo?: string | null;
  anio?: number | null;
  tipo_equipo?: string;
};

export type ParamsPersonal = ParamsPagina & {
  contrato_id?: string;
  faena_id?: string;
  estado?: EstadoSujeto;
  cargo_id?: string;
  cargo?: string;
  es_conductor?: boolean;
};

export type ParamsEquipos = ParamsPagina & {
  contrato_id?: string;
  faena_id?: string;
  estado?: EstadoSujeto;
  tipo_equipo?: string;
};

// ============================================================================
// Cargos
// ============================================================================
export type RequisitoCargo = {
  template_id: string;
  titulo: string;
  obligatorio: boolean;
};

export type Cargo = {
  id: string;
  nombre: string;
  categoria: CategoriaCargo;
  requiere_emsipor: boolean;
  activo: boolean;
  es_global: boolean;
  requisitos: RequisitoCargo[];
  trabajadores: number;
  cumplimiento_pct: number;
};

export type CargoIn = {
  nombre: string;
  categoria?: CategoriaCargo;
  requiere_emsipor?: boolean;
  requisitos?: { template_id: string; obligatorio?: boolean }[];
};

export type CargoPatch = {
  nombre?: string;
  categoria?: CategoriaCargo;
  requiere_emsipor?: boolean;
  activo?: boolean;
  /** `null` = no tocar la lista; `[]` = dejar el cargo sin requisitos propios. */
  requisitos?: { template_id: string; obligatorio?: boolean }[];
};

export type RequisitosCargo = {
  cargo: {
    id: string;
    nombre: string;
    requiere_emsipor: boolean;
    es_global: boolean;
  };
  personal: {
    template_id: string;
    titulo: string;
    ambito: Ambito;
    tipo: TipoRequisito | null;
    obligatorio: boolean;
    vigencia_meses: number | null;
    plataforma: string | null;
    ejemplo_clave: string | null;
  }[];
  emsipor: RequisitosCargo["personal"];
  total: number;
};

// ============================================================================
// Licencia interna de mina (EMSIPOR)
// ============================================================================
export type LicenciaInterna = {
  sujeto: {
    id: string;
    nombre: string;
    rut: string | null;
    cargo: string | null;
    es_conductor: boolean;
    contrato_id: string;
  };
  numero: string | null;
  /** Limitación: una licencia caducada llega como `por_vencer`. */
  estado: EstadoLicencia;
  vence: string | null;
  dias_para_vencer: number | null;
  emsipor_estado: EstadoEmsipor;
  checklist: Documento[];
  resumen: { total: number; ok: number; faltan: number };
  reset?: {
    documentos_eliminados: number;
    archivos_eliminados: number;
    documentos_creados: number;
  };
};

// ============================================================================
// Alertas
// ============================================================================
export type Alerta = {
  id: string;
  severidad: Severidad;
  estado: EstadoAlerta;
  origen: OrigenAlerta;
  titulo: string;
  descripcion: string | null;
  plataforma: string | null;
  documento_id: string | null;
  sujeto_id: string | null;
  contrato_id: string | null;
  leida: boolean;
  resuelta: boolean;
  leida_at: string | null;
  resuelta_at: string | null;
  created_at: string;
};

export type ResumenAlertas = {
  criticas: number;
  advertencias: number;
  informativas: number;
  resueltas_30d: number;
  no_leidas: number;
  activas: number;
};

export type AlertaPatch = {
  leida?: boolean;
  resuelta?: boolean;
  estado?: EstadoAlerta;
};

export type ParamsAlertas = ParamsPagina & {
  severidad?: Severidad;
  origen?: OrigenAlerta;
  estado?: EstadoAlerta;
  contrato_id?: string;
  sujeto_id?: string;
  leida?: boolean;
  solo_activas?: boolean;
};

// ============================================================================
// Dashboard y actividad
// ============================================================================
export type Kpis = {
  contratos_activos: number;
  faenas_activas: number;
  personal: { acreditados: number; total: number };
  equipos: { acreditados: number; total: number };
  documentos: { ok: number; total: number };
  cumplimiento_general_pct: number;
  alertas: { criticas: number; advertencias: number };
};

export type CumplimientoContrato = StatsContrato & {
  id: string;
  nombre: string;
  codigo: string | null;
  faena: string;
  faena_id: string;
  estado: EstadoContrato;
};

export type AcreditacionesEstado = {
  acreditados: number;
  pendientes: number;
  vencidos: number;
  total: number;
};

export type ActividadFeed = {
  id: number;
  tipo: TipoActividad;
  modulo: string;
  descripcion: string;
  entidad_tipo: string | null;
  entidad_id: string | null;
  created_at: string;
};

export type ActividadFila = ActividadFeed & {
  plataforma: string | null;
  usuario: { id?: string; nombre: string | null; email?: string; rol?: Rol };
};

export type ProximoVencimiento = {
  documento_id: string;
  titulo: string;
  vence: string;
  dias: number;
  sujeto_id: string | null;
  sujeto: string | null;
  estado_calc: EstadoCalc;
};

export type PuntoTendencia = {
  periodo_inicio: string;
  fecha: string;
  cumplimiento_pct: number;
  docs_ok: number;
  docs_total: number;
  personal_acreditados: number;
  personal_total: number;
  equipos_acreditados: number;
  equipos_total: number;
  alertas_criticas: number;
};

/**
 * `anterior` y `delta_pct` **no aparecen** si no hay dos periodos con
 * snapshots, y `actual` falta con la serie vacía: hay que comprobar la
 * presencia de las claves, no asumir `delta_pct = 0`.
 */
export type Tendencia = {
  periodo: Periodo;
  desde: string;
  hasta: string;
  contrato_id: string | null;
  faena_id: string | null;
  snapshots_leidos: number;
  actual?: PuntoTendencia;
  anterior?: PuntoTendencia;
  delta_pct?: number;
  serie: PuntoTendencia[];
  nota?: string;
};

// ============================================================================
// Administración de la plataforma
// ============================================================================
export type Empresa = {
  id: string;
  nombre: string;
  rut: string;
  email: string;
  status: EstadoEmpresa;
  es_demo: boolean;
  rejection_reason: string | null;
  approved_at: string | null;
  created_at: string;
  contratos: number;
  cumplimiento_pct: number;
  docs_ok: number;
  docs_total: number;
};

export type EmpresaDetalle = Empresa & {
  stats: {
    usuarios: number;
    usuarios_activos: number;
    contratos: number;
    contratos_vigentes: number;
    personal: { total: number; acreditados: number };
    equipos: { total: number; acreditados: number };
    documentos: Record<EstadoCalc | "total", number>;
    alertas_activas: number;
    cumplimiento_pct: number;
  };
};

export type AdminStats = {
  empresas_total: number;
  pendientes: number;
  aprobadas: number;
  rechazadas: number;
  contratos_total: number;
  faenas_total: number;
  faenas_activas: number;
  plantillas_activas: number;
  personal: { total: number; acreditados: number };
  equipos: { total: number; acreditados: number };
  sujetos_total: number;
  documentos_total: number;
  documentos_ok: number;
  /** Media simple por empresa aprobada. */
  cumplimiento_promedio: number;
  /** Media ponderada por documentos. Es otro número, a propósito. */
  cumplimiento_global_pct: number;
  empresas_medidas: number;
};

export type PlantillaAdmin = PlantillaRequisito & {
  es_estandar: boolean;
  aplica_a: string | null;
  activo: boolean;
  created_at: string | null;
  updated_at: string | null;
  documentos_instanciados?: number;
  nota?: string;
};

export type FaenaAdmin = {
  id: string;
  nombre: string;
  mandante: string;
  grupo: string | null;
  region: string | null;
  sector: string;
  activa: boolean;
  logo_url: string | null;
  color: string | null;
  lat: number | null;
  lng: number | null;
  created_at: string | null;
  plataformas?: number;
};

export type ResetDemo = {
  borrados: Record<string, number>;
  creados: Record<string, number>;
  documentos_por_estado: Record<string, number>;
  nota: string;
};

// ============================================================================
// Empresa (autogestión) y usuarios
// ============================================================================
export type UsuarioEmpresa = {
  id: string;
  nombre: string | null;
  email: string;
  role: Rol;
  activo: boolean;
  status: EstadoEmpresa;
  contrato_id: string | null;
  contrato?: ContratoResumen | null;
  last_login_at: string | null;
  created_at: string | null;
};

// ============================================================================
// Personas y flota (vistas consolidadas por RUT / patente)
// ============================================================================
export type Persona = {
  rut?: string;
  patente?: string;
  nombre: string;
  registros: number;
  activos: number;
  faenas: string[];
  contratos: { id: string; nombre: string; faena: string }[];
  cumplimiento_pct: number;
  docs_ok: number;
  docs_total: number;
  proximo_vencimiento: { titulo: string; vence: string; dias: number } | null;
  certificaciones_principales: { titulo: string; vence: string | null }[];
  cargo?: string | null;
  cargo_id?: string | null;
  es_conductor?: boolean;
  tipo_equipo?: string | null;
  marca?: string | null;
  modelo?: string | null;
  anio?: number | null;
};

// ============================================================================
// Calendario
// ============================================================================
export type EventoCalendario = {
  id: string | null;
  categoria: CategoriaEvento;
  titulo: string;
  fecha: string;
  descripcion: string | null;
  completado: boolean;
  documento_id: string | null;
  estado_calc: EstadoCalc | null;
  dias: number;
  sujeto: { id: string; nombre: string; tipo: TipoSujeto } | null;
  contrato: { id: string; nombre: string; faena: string } | null;
  editable: boolean;
  created_at: string | null;
};

export type Eventos = {
  items: EventoCalendario[];
  total: number;
  por_categoria: Record<CategoriaEvento, number>;
  desde: string;
  hasta: string;
  truncado: boolean;
};

// ============================================================================
// Reportes y exportaciones
// ============================================================================
export type Reporte = {
  id: string;
  nombre: string;
  tipo: TipoReporte;
  formato: "pdf" | "excel";
  status: EstadoJob;
  params: Record<string, unknown>;
  created_at: string | null;
  updated_at?: string | null;
  generado_por?: { id?: string; nombre: string | null; email?: string };
  error?: string | null;
};

export type ReporteProgramado = {
  id: string;
  nombre: string;
  tipo: TipoReporte;
  formato: "pdf" | "excel";
  params: Record<string, unknown>;
  cron_expr: string;
  activo: boolean;
  ultima_ejecucion_at?: string | null;
  created_at: string | null;
};

export type Exportacion = {
  download_url?: string;
  expires_at?: string;
  filas?: number;
  filename?: string;
  formato?: string;
  degradado_a_csv?: boolean;
  /** Derivada a job: se descarga con `reportes.downloadUrl(id)`. */
  id?: string;
  status?: EstadoJob;
  filas_estimadas?: number;
  nota?: string;
};

// ============================================================================
// Plataformas del contrato y credenciales
// ============================================================================
export type PlataformaContrato = {
  id: string;
  nombre: string;
  descripcion: string | null;
  url: string | null;
  color: string | null;
  nota: string | null;
  orden: number;
  estado: EstadoAcceso;
  solicitado_at: string | null;
  habilitado_at: string | null;
  es_custom: boolean;
  heredado?: boolean;
  faena_plataforma_id?: string | null;
  credenciales?: number;
};

export type Credencial = {
  id: string;
  nombre: string;
  usuario: string;
  estado: "activa" | "revocada" | "por_vencer";
  version: number;
  vence_at: string | null;
  created_at: string | null;
  ultimo_uso_at?: string | null;
};

// ============================================================================
// Integraciones
// ============================================================================
export type Integracion = {
  id: string;
  tipo: TipoIntegracion;
  nombre: string;
  estado: EstadoIntegracion;
  config: Record<string, unknown>;
  ultima_sync_at: string | null;
  syncs_exitosas: number;
  syncs_fallidas: number;
  registros_sincronizados: number;
  ultimo_error: string | null;
  created_at: string | null;
};

export type SyncLog = {
  id: string;
  status: "exito" | "error";
  mensaje: string | null;
  registros_procesados: number;
  started_at: string | null;
  finished_at: string | null;
  duracion_seg: number | null;
};

// ============================================================================
// Requisitos (catálogo con el estado de la empresa)
// ============================================================================
export type RequisitoFila = {
  template_id: string;
  titulo: string;
  ambito: Ambito;
  tipo: TipoRequisito | null;
  obligatorio: boolean;
  plataforma: string | null;
  ejemplo_clave: string | null;
  vigencia_meses: number | null;
  estado: EstadoCalc;
  docs: number;
  ok: number;
  porvenc: number;
  venc: number;
  falta: number;
};
