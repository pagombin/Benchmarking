// Shared API shapes. Kept in lock-step with the FastAPI JSON responses.

export type Role = "viewer" | "operator" | "admin";

export interface Me {
  user: string;
  role: Role;
  version: string;
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
