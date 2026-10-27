CREATE SCHEMA IF NOT EXISTS assessments;
REVOKE ALL ON SCHEMA assessments FROM PUBLIC;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS assessments.snapshots (
    tenant_id text NOT NULL,
    snapshot_id text NOT NULL,
    scope_hash text NOT NULL,
    subscription_ids jsonb NOT NULL,
    stale_days integer NOT NULL,
    created_at timestamptz NOT NULL,
    summary jsonb NOT NULL,
    blob_name text NOT NULL,
    published boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS snapshots_latest
    ON assessments.snapshots (tenant_id, scope_hash, created_at DESC)
    WHERE published;
ALTER TABLE assessments.snapshots ADD COLUMN IF NOT EXISTS period_start text NOT NULL DEFAULT '';
UPDATE assessments.snapshots SET period_start=summary#>>'{reportMetadata,periodStart}'
    WHERE period_start='' AND summary#>>'{reportMetadata,periodStart}' IS NOT NULL;
CREATE INDEX IF NOT EXISTS snapshots_period
    ON assessments.snapshots (tenant_id,scope_hash,period_start,created_at DESC) WHERE published;

CREATE TABLE IF NOT EXISTS assessments.collections (
    tenant_id text NOT NULL,
    snapshot_id text NOT NULL,
    path text NOT NULL,
    record_count bigint NOT NULL CHECK (record_count >= 0),
    PRIMARY KEY (tenant_id, snapshot_id, path),
    FOREIGN KEY (tenant_id, snapshot_id)
        REFERENCES assessments.snapshots ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS assessments.records (
    tenant_id text NOT NULL,
    snapshot_id text NOT NULL,
    path text NOT NULL,
    ordinal bigint NOT NULL,
    subscription_id text NOT NULL,
    resource_id text NOT NULL,
    search_text text NOT NULL,
    payload jsonb NOT NULL,
    payload_bytes integer NOT NULL CHECK (payload_bytes > 0),
    PRIMARY KEY (tenant_id, snapshot_id, path, ordinal),
    FOREIGN KEY (tenant_id, snapshot_id, path)
        REFERENCES assessments.collections ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS records_subscription
    ON assessments.records (tenant_id, snapshot_id, path, subscription_id, ordinal);
CREATE INDEX IF NOT EXISTS records_resource
    ON assessments.records (tenant_id, snapshot_id, resource_id);
CREATE INDEX IF NOT EXISTS records_search
    ON assessments.records USING gin (search_text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS assessments.jobs (
    job_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    requested_by text NOT NULL,
    scope_hash text NOT NULL,
    subscription_ids jsonb NOT NULL,
    stale_days integer NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    phase text NOT NULL DEFAULT 'queued',
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token uuid,
    attempts integer NOT NULL DEFAULT 0,
    snapshot_id text,
    error_code text,
    error_message text
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_scope
    ON assessments.jobs (tenant_id, scope_hash)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_claim
    ON assessments.jobs (next_attempt_at, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_scope_history
    ON assessments.jobs (tenant_id, scope_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS assessments.azure_request_budgets (
    scope_key text PRIMARY KEY,
    next_at timestamptz NOT NULL
);

REVOKE ALL ON ALL TABLES IN SCHEMA assessments FROM PUBLIC;
