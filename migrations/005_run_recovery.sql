-- M5: explicit ownership, bounded call reservations and checked checkpoints.
ALTER TABLE research_runs DROP CONSTRAINT research_runs_status_check;
ALTER TABLE research_runs DROP CONSTRAINT research_runs_check;
ALTER TABLE research_runs ADD CONSTRAINT research_runs_status_m5 CHECK
 (status IN ('queued','running','completed','completed_with_gaps','failed','interrupted','cancelled'));
ALTER TABLE research_runs ADD CONSTRAINT research_runs_terminal_m5 CHECK
 ((status IN ('completed','completed_with_gaps','failed','interrupted','cancelled')) = (finished_at IS NOT NULL));
ALTER TABLE research_runs ADD COLUMN idempotency_key text CHECK
 (idempotency_key IS NULL OR idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$');
ALTER TABLE research_runs ADD COLUMN request_fingerprint text CHECK
 (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE research_runs ADD COLUMN workflow_config_fingerprint text CHECK
 (workflow_config_fingerprint IS NULL OR workflow_config_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE research_runs ADD COLUMN owner_token uuid;
ALTER TABLE research_runs ADD COLUMN lease_until timestamptz;
ALTER TABLE research_runs ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
ALTER TABLE research_runs ADD COLUMN deadline_at timestamptz;
ALTER TABLE research_runs ADD COLUMN resume_count integer NOT NULL DEFAULT 0 CHECK (resume_count >= 0);
ALTER TABLE research_runs ADD COLUMN unknown_acknowledged_at timestamptz;
CREATE UNIQUE INDEX research_runs_idempotency ON research_runs(data_root,idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX research_runs_root_history ON research_runs(data_root,created_at DESC,run_id DESC);
CREATE INDEX research_runs_root_ticker_status ON research_runs(data_root,ticker,status,created_at DESC,run_id DESC);
CREATE INDEX research_runs_expiring ON research_runs(data_root,lease_until) WHERE status='running';
ALTER TABLE run_steps ADD COLUMN owner_token uuid;
ALTER TABLE run_steps ADD COLUMN artifact_path text;
ALTER TABLE run_steps ADD COLUMN artifact_sha256 text CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE run_steps ADD COLUMN workflow_fingerprint text CHECK (workflow_fingerprint IS NULL OR workflow_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE run_steps ADD COLUMN verified_at timestamptz;
CREATE TABLE run_attempts (
 run_id text NOT NULL REFERENCES research_runs(run_id),
 role text NOT NULL CHECK (role IN ('fundamentals','events','risk','synthesis')),
 role_attempt integer NOT NULL CHECK (role_attempt BETWEEN 1 AND 2),
 total_ordinal integer NOT NULL CHECK (total_ordinal BETWEEN 1 AND 8),
 owner_token uuid NOT NULL,
 status text NOT NULL CHECK (status IN ('reserved','completed','failed','unknown')),
 usage jsonb CHECK (usage IS NULL OR jsonb_typeof(usage)='object'),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz,
 PRIMARY KEY(run_id,role,role_attempt), UNIQUE(run_id,total_ordinal),
 CHECK ((status='reserved') = (finished_at IS NULL))
);
