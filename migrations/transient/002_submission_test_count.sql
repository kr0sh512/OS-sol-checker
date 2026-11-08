ALTER TABLE submissions ADD COLUMN total_tests integer;

-- Recover the historical count only from the same submission/job/snapshot.
-- Never substitute the current catalog when old proof metadata was already purged.
UPDATE submissions AS s SET total_tests = COALESCE(
    (s.result->>'total_tests')::integer,
    (s.proven_result->>'total_tests')::integer,
    (SELECT j.total_tests FROM queue_jobs AS j WHERE j.id = s.queue_job_id),
    (SELECT jsonb_array_length(task->'tests')
       FROM catalog_snapshots AS c,
            LATERAL jsonb_array_elements(c.snapshot->'topics') AS topic,
            LATERAL jsonb_array_elements(topic->'tasks') AS task
      WHERE c.snapshot_id = s.context->>'snapshot_id' AND task->>'id' = s.task_id
      LIMIT 1),
    0
);

ALTER TABLE submissions ALTER COLUMN total_tests SET NOT NULL;
ALTER TABLE submissions ADD CONSTRAINT submissions_total_tests_nonnegative CHECK (total_tests >= 0);
