CREATE TABLE blobs (
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 0 AND 102400),
    source bytea NOT NULL CHECK (octet_length(source) = byte_size),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(sha256, byte_size)
);
CREATE TABLE accepted_solutions (
    id uuid PRIMARY KEY,
    task_id text NOT NULL,
    tests_digest text NOT NULL CHECK (tests_digest ~ '^[0-9a-f]{64}$'),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    sha256 text NOT NULL,
    byte_size integer NOT NULL,
    context jsonb NOT NULL,
    first_accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(sha256, byte_size) REFERENCES blobs(sha256, byte_size),
    UNIQUE(task_id, tests_digest, policy_digest, sha256, byte_size)
);
CREATE TABLE success_receipts (
    submission_id uuid PRIMARY KEY,
    accepted_solution_id uuid NOT NULL REFERENCES accepted_solutions(id),
    context jsonb NOT NULL,
    author_name text NOT NULL CHECK (length(author_name) BETWEEN 1 AND 120),
    author_group text NOT NULL CHECK (length(author_group) BETWEEN 1 AND 64),
    submitted_at timestamptz NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION checker_archive_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'historical archive rows are immutable' USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER checker_blobs_immutable BEFORE UPDATE OR DELETE ON blobs
    FOR EACH ROW EXECUTE FUNCTION checker_archive_immutable();
CREATE TRIGGER checker_acceptances_immutable BEFORE UPDATE OR DELETE ON accepted_solutions
    FOR EACH ROW EXECUTE FUNCTION checker_archive_immutable();
CREATE TRIGGER checker_receipts_immutable BEFORE UPDATE OR DELETE ON success_receipts
    FOR EACH ROW EXECUTE FUNCTION checker_archive_immutable();
