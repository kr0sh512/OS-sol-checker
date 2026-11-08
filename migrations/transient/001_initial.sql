CREATE TABLE checker_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    catalog jsonb,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    tests_digest text,
    policy jsonb,
    policy_digest text,
    CHECK ((catalog IS NULL AND generation = 0 AND tests_digest IS NULL)
        OR (catalog IS NOT NULL AND generation > 0 AND tests_digest ~ '^[0-9a-f]{64}$')),
    CHECK ((policy IS NULL) = (policy_digest IS NULL))
);
INSERT INTO checker_state(singleton) VALUES (true);

CREATE TABLE catalog_snapshots (
    snapshot_id text PRIMARY KEY,
    snapshot jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    token_hash text UNIQUE,
    name text CHECK (name IS NULL OR length(name) BETWEEN 1 AND 120),
    "group" text CHECK ("group" IS NULL OR length("group") BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    CHECK ((name IS NULL) = ("group" IS NULL))
);
CREATE INDEX sessions_expiry ON sessions(expires_at);

CREATE TABLE queue_jobs (
    id uuid PRIMARY KEY,
    submission_id uuid NOT NULL,
    task_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 0 AND 102400),
    source bytea,
    context jsonb NOT NULL,
    total_tests integer NOT NULL CHECK (total_tests >= 0),
    state text NOT NULL CHECK (state IN ('queued', 'claimed', 'finished', 'invalidated')),
    stage text NOT NULL CHECK (stage IN ('queued', 'compiling', 'running', 'finished', 'invalidated')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    finished_at timestamptz,
    scheduler_id uuid,
    execution_deadline timestamptz,
    result jsonb,
    state_reason text,
    CHECK (source IS NULL OR octet_length(source) = byte_size),
    CHECK (state NOT IN ('queued', 'claimed') OR source IS NOT NULL),
    CHECK (state <> 'claimed' OR (scheduler_id IS NOT NULL AND claimed_at IS NOT NULL AND execution_deadline IS NOT NULL))
);
CREATE UNIQUE INDEX queue_jobs_active_identity
    ON queue_jobs(task_id, generation, policy_digest, sha256, byte_size)
    WHERE state IN ('queued', 'claimed');
CREATE INDEX queue_jobs_fifo ON queue_jobs(created_at, id) WHERE state = 'queued';
CREATE INDEX queue_jobs_source_identity ON queue_jobs(sha256, byte_size) WHERE source IS NOT NULL;

CREATE TABLE submissions (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id),
    author_name text NOT NULL CHECK (length(author_name) BETWEEN 1 AND 120),
    author_group text NOT NULL CHECK (length(author_group) BETWEEN 1 AND 64),
    task_id text NOT NULL,
    context jsonb NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 0 AND 102400),
    source bytea,
    original_filename text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    expires_at timestamptz,
    status text NOT NULL CHECK (status IN ('queued', 'compiling', 'running', 'archive_pending', 'finished', 'invalidated')),
    result jsonb,
    proven_result jsonb,
    cache_hit boolean NOT NULL DEFAULT false,
    queue_job_id uuid REFERENCES queue_jobs(id) ON DELETE SET NULL,
    state_reason text,
    archive_receipt_confirmed boolean NOT NULL DEFAULT false,
    CHECK (source IS NULL OR octet_length(source) = byte_size),
    CHECK (status = 'invalidated' OR source IS NOT NULL),
    CHECK ((status = 'finished') = (result IS NOT NULL)),
    CHECK ((status IN ('finished', 'invalidated')) = (finished_at IS NOT NULL AND expires_at IS NOT NULL)),
    CHECK ((status = 'archive_pending') = (proven_result IS NOT NULL)),
    CHECK (proven_result IS NULL OR proven_result->>'verdict' = 'accepted'),
    CHECK (result IS NULL OR result->>'verdict' <> 'accepted' OR archive_receipt_confirmed)
);
CREATE INDEX submissions_expiry ON submissions(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX submissions_followers ON submissions(queue_job_id);
CREATE INDEX submissions_archive_pending ON submissions(created_at, id) WHERE status = 'archive_pending';
CREATE INDEX submissions_source_identity ON submissions(sha256, byte_size) WHERE source IS NOT NULL;
CREATE INDEX submissions_pending_identity
    ON submissions(task_id, generation, policy_digest, sha256, byte_size)
    WHERE status = 'archive_pending';

CREATE TABLE session_last_submissions (
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id text NOT NULL,
    submission_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(session_id, task_id)
);
CREATE FUNCTION checker_last_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM submissions WHERE id = NEW.submission_id
                   AND session_id = NEW.session_id AND task_id = NEW.task_id) THEN
        RAISE EXCEPTION 'last submission owner mismatch' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER checker_last_owner BEFORE INSERT OR UPDATE ON session_last_submissions
    FOR EACH ROW EXECUTE FUNCTION checker_last_owner();

CREATE TABLE result_cache (
    task_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 0 AND 102400),
    source bytea NOT NULL CHECK (octet_length(source) = byte_size),
    context jsonb NOT NULL,
    result jsonb NOT NULL CHECK (result->>'verdict' IN ('accepted', 'compile_error', 'compile_timeout',
        'runtime_error', 'run_timeout', 'wrong_answer', 'memory_limit', 'output_limit', 'submission_timeout')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(task_id, generation, policy_digest, sha256, byte_size)
);
CREATE INDEX result_cache_expiry ON result_cache(expires_at);
CREATE INDEX result_cache_source_identity ON result_cache(sha256, byte_size);

CREATE TABLE worker_status (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    scheduler_id uuid NOT NULL,
    heartbeat_at timestamptz NOT NULL
);
