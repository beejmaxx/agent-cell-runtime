CREATE TABLE IF NOT EXISTS tenants (
    id uuid PRIMARY KEY,
    slug text UNIQUE NOT NULL,
    mw_issuer text UNIQUE NOT NULL,
    mw_base_url text NOT NULL,
    mw_host text NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    client_id text NOT NULL,
    operations text[] NOT NULL,
    PRIMARY KEY (tenant_id, name)
);
CREATE TABLE IF NOT EXISTS executions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    principal_account_id text NOT NULL,
    agent text NOT NULL,
    grant_id text NOT NULL,
    operations text[] NOT NULL,
    input jsonb NOT NULL,
    status text NOT NULL CHECK (status IN
        ('PENDING','PROVISIONING','RUNNING','SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')),
    failure_reason text CHECK (failure_reason IN
        ('LAUNCH_FAILED','LAUNCH_UNKNOWN','POD_LOST','EXITED_WITHOUT_COMPLETION')),
    workload_name text UNIQUE NOT NULL,
    workload_uid text,
    launch_attempted_at timestamptz,
    deadline_at timestamptz NOT NULL,
    credential_hash text,
    result jsonb,
    result_hash text,
    created_at timestamptz NOT NULL,
    finished_at timestamptz,
    FOREIGN KEY (tenant_id, agent) REFERENCES agents(tenant_id, name),
    CHECK ((status IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')) =
           (finished_at IS NOT NULL)),
    CHECK ((status = 'SUCCEEDED') = (result IS NOT NULL AND result_hash IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    principal_account_id text NOT NULL,
    idem_key text NOT NULL,
    request_hash text NOT NULL,
    execution_id uuid NOT NULL REFERENCES executions(id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, principal_account_id, idem_key)
);
