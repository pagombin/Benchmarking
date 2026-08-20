// Shared API shapes. Kept in lock-step with the FastAPI JSON responses.

export type Role = "viewer" | "operator" | "admin";

export interface Me {
  user: string;
  role: Role;
  version: string;
  sha?: string;
}

export interface WorkerStatus {
  worker_alive: boolean;
  active_jobs: number;
  queued_jobs: number;
}

export interface Run {
  run_id: string;
  label: string;
  edition: string;
  tshirt_size: string;
  mode: string;
  workload_type: string;
  status: string;
  tags: string;
  ticket: string;
  owner: string;
  environment: string;
  peak_qps: number | null;
  created_utc: string;
  finished_utc: string;
  source: string;
  // host/cluster are surfaced in Phase 3 (targets); present-but-empty until then.
  target_host?: string;
}

export interface Target {
  id: number;
  name: string;
  host: string;
  port: number;
  dbname: string;
  dbuser: string;
  sslmode: string;
}

export interface Job {
  id: number;
  kind: string;
  state: string;
  run_id: string | null;
  requested_by: string;
  scheduled_utc: string | null;
  created_utc: string;
  started_utc?: string | null;
  finished_utc?: string | null;
  exit_code?: number | null;
  error: string | null;
}

export interface PrepareStats {
  loaded_units?: string;
  wall_s?: number;
  db_size_pretty?: string;
  load_mb_s?: number | null;
  load_threads?: number;
  started_utc?: string;
  finished_utc?: string;
  database?: string;
  target_host?: string;
}

// ── continuous mode ──

export interface ContSupervisor {
  status?: string;
  seg?: number;
  segments_total?: number;
  relaunches?: number;
  consecutive_failures?: number;
  last_error_class?: string;
  updated_utc?: string;
  load_stopped?: boolean;
}

export interface ContOpenOutage { id: number; kind: string; started_utc: string; planned: number }
export interface ContOpenAlert { id: number; type: string; severity: string; fired_utc: string }

export interface ContJob {
  id: number;
  kind: string;
  state: string;
  desired_state: string;
  run_id: string | null;
  target_id: number | null;
  requested_by: string;
  created_utc: string;
  started_utc?: string | null;
  finished_utc?: string | null;
  exit_code?: number | null;
  error: string | null;
  target_name: string | null;
  target_host: string | null;
  last_sample_utc: string;
  open_outages: ContOpenOutage[];
  open_alerts: ContOpenAlert[];
  supervisor: ContSupervisor | null;
  workload_type: string;
  threads: number | null;
  label: string;
  spark?: { t: number[]; tps: (number | null)[] };
  uptime_24h_pct?: number | null;
  tps_avg_24h?: number | null;
  manifest?: { status: string; continuous: Record<string, unknown> } | null;
}

export interface Outage {
  id: number;
  job_id: number;
  kind: string;                 // read | write | load
  started_utc: string;
  ended_utc: string | null;
  duration_s: number | null;
  error_class: string | null;
  first_error: string | null;
  planned: number;
}

export interface AlertRow {
  id: number;
  job_id: number | null;
  type: string;
  severity: "info" | "warn" | "crit";
  fired_utc: string;
  resolved_utc: string | null;
  dedup_key: string;
  context: string;
  delivery: string | null;
  delivery_attempts: number;
  delivered_utc: string | null;
}

export interface MaintWindow {
  id: number;
  job_id: number | null;
  starts_utc: string;
  ends_utc: string;
  note: string | null;
}

export interface ContEvent { ts_utc: string; type: string; label: string; note?: string }

export interface ContTimeseries {
  job_id: number;
  from_utc: string;
  to_utc: string;
  resolution: "1s" | "1m";
  points: number;
  t: number[];
  tps_avg: (number | null)[];
  tps_min: (number | null)[];
  qps_avg: (number | null)[];
  lat_p99_avg: (number | null)[];
  lat_p99_max: (number | null)[];
  err_sum: (number | null)[];
  reconn_sum: (number | null)[];
  gap_s: (number | null)[];
  outages: Outage[];
  maintenance: MaintWindow[];
  events: ContEvent[];
}

export interface ContSummary {
  job_id: number;
  from_utc: string;
  to_utc: string;
  window_s: number;
  uptime_pct: number | null;
  downtime_s: number;
  outages_total: number;
  outages_unplanned_db: number;
  outages_load: number;
  outages_planned: number;
  outages_open: number;
  mtbf_s: number | null;
  mttr_s: number | null;
  longest_outage_s: number;
  observed_seconds: number;
  gap_seconds: number;
  tps_avg: number | null;
  errors_total: number;
  reconnects_total: number;
  lat_p99_p50: number | null;
  lat_p99_p95: number | null;
  lat_p99_p99: number | null;
  lat_p99_max: number | null;
  harness_relaunches: number;
}

export interface ContDbMetrics {
  t: number[];
  points: number;
  conn_active: (number | null)[];
  conn_idle: (number | null)[];
  conn_idle_tx: (number | null)[];
  conn_total: (number | null)[];
  repl_lag_s: (number | null)[];
  repl_count: (number | null)[];
  db_size: (number | null)[];
  dead_tup: (number | null)[];
  ckpt_timed: (number | null)[];
  ckpt_req: (number | null)[];
  xact_commit_rate: (number | null)[];
  xact_rollback_rate: (number | null)[];
  blks_hit_rate: (number | null)[];
  blks_read_rate: (number | null)[];
  wal_bytes_rate: (number | null)[];
  archived_count_rate: (number | null)[];
  archive_failed_rate: (number | null)[];
  deadlocks_rate: (number | null)[];
  temp_bytes_rate: (number | null)[];
  wal_records_rate: (number | null)[];
  top_queries: { q: string; calls: number; total_ms: number }[] | null;
}

// ── cluster ops ──

export interface KubeTarget {
  id: number;
  name: string;
  kubeconfig_path: string;
  kubeconfig_imported: boolean;
  context: string;
  namespace: string;
  cr_kind: string;
  cr_name: string;
  pguser_secret: string;
  pguser_secret_key: string;
  db_user: string;
  db_name: string;
  api_server: string;
  last_validated_utc: string | null;
  last_validation_ok: boolean | null;
  topology_utc: string | null;
  params_utc: string | null;
  health_utc: string | null;
  health_status: string | null;
  auto_health_s: number;
  schedules_paused: boolean;
  schedules_paused_utc: string | null;
  created_utc: string;
}

// ── parameter map (introspected pg_settings + apply-channel overlay) ──

export interface PgParam {
  name: string;
  setting: string | null;
  unit: string | null;
  vartype: string;                 // bool | enum | integer | real | string
  min_val: string | null;
  max_val: string | null;
  enumvals: string[];
  context: string;                 // internal|postmaster|sighup|superuser|user|backend...
  category: string;
  short_desc: string;
  boot_val: string | null;
  reset_val: string | null;
  source: string | null;
  pending_restart: boolean;
  channel: "cr" | "dcs-coordinated" | "patroni-locked" | "operator-managed" | "readonly";
  restart_required: boolean;
  cr_value: string | null;
}

export interface PgParamsCatalog {
  collected_utc: string;
  leader: string;
  pg_version: string;
  params: PgParam[];
  cr_managed: Record<string, string>;
  pgbackrest_global: Record<string, string>;
  pgbouncer_global?: Record<string, string>;
  /** live DCS document (patronictl show-config) overlaid on the CR view */
  patroni_dcs?: Record<string, string>;
  /** CR-derived view only — for flagging CR-vs-DCS drift */
  patroni_dcs_cr?: Record<string, string>;
}

// ── diagnostics workbench ──

export interface DiagCheckInfo {
  key: string;
  title: string;
  description: string;
  category: string;
  kind: string;
  columns: string[];
  watch: boolean;
  chart: string;
}

// ── health checks ──

export interface HealthFinding {
  id: string;
  severity: "ok" | "info" | "warn" | "crit";
  title: string;
  value: string;
  detail: string;
  remediation: string;
  action: { type?: string; checks?: string[]; filter?: string };
}

export interface HealthDoc {
  collected_utc: string;
  status: string;
  findings: HealthFinding[];
  checked: number;
  leader: string;
  metrics?: Record<string, number>;
}

export interface PatroniMember {
  name: string;
  host: string;
  role: string;
  state: string;
  timeline: number | null;
  lag_mb: number | null;
}

export interface Topology {
  collected_utc: string;
  namespace: string;
  cr_kind: string;
  cr_name: string;
  postgres_version?: string;
  patroni?: { leader: string; timeline: number | null; members: PatroniMember[]; error?: string };
  pods?: { instances: PodInfo[]; pgbouncer: PodInfo[]; backup_jobs: PodInfo[]; other: PodInfo[] };
  statefulsets?: { name: string; replicas: number | null; ready: number }[];
  services?: { name: string; type: string; cluster_ip: string }[];
  backups?: { schedules: { repo: string; schedules: Record<string, string> }[];
              manual: unknown; global: Record<string, string> };
  pgbackrest_info?: string;
}

export interface PodInfo {
  name: string;
  phase: string;
  ready: string;
  node: string;
  pod_ip: string;
  containers?: string[];
}

export interface CrSnapshot {
  op_run_id: string;
  op: string;
  action: string;
  created_utc: string;
  status: string;
  diff_summary: string;
  diff_total: number;
}

export interface OpsRun {
  op_run_id: string;
  kind: string;
  kube_target_id: number | null;
  kube_target_name: string;
  label: string;
  params: Record<string, unknown>;
  status: string;
  linked_run_id: string;
  headline: Record<string, any>;
  created_utc: string;
  finished_utc: string;
}

export interface OpsRunDetail {
  meta: Record<string, any>;
  index: OpsRun | null;
  job_id: number | null;
  job_state: string | null;
  stitched: Record<string, any> | null;
  files: string[];
  raw_files: string[];
}

export interface OpsCompareRow {
  op_run_id: string;
  kind: string;
  label: string;
  target: string;
  created_utc: string;
  case: string;
  downtime_ms: number | null;
  detection_ms: number | null;
  flip: boolean | null;
  classification: string;
  tl_change: string;
  new_primary: string;
  backoff_tail_ms: number | null;
  full_ha_recovery_s: number | null;
  status: string;
}
