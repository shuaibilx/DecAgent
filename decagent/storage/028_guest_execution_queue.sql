-- Durable queue state shares the guest task admission ledger so enqueue and the
-- daily task count can commit or roll back together.
ALTER TABLE business.guest_trial_task_admissions
 ADD COLUMN queue_state text NOT NULL DEFAULT 'cancelled'
  CHECK (queue_state IN ('waiting','running','paused','succeeded','failed','cancelled')),
 ADD COLUMN enqueued_at timestamptz NOT NULL DEFAULT now(),
 ADD COLUMN last_claimed_at timestamptz;

UPDATE business.guest_trial_task_admissions AS admission
SET queue_state = CASE task.state
      WHEN 'queued' THEN 'waiting'
      WHEN 'running' THEN 'running'
      WHEN 'succeeded' THEN 'succeeded'
      WHEN 'failed' THEN 'failed'
      WHEN 'cancelled' THEN 'cancelled'
      ELSE 'paused'
    END,
    enqueued_at = coalesce(task.created_at, admission.admitted_at),
    last_claimed_at = CASE WHEN task.state='running' THEN task.created_at ELSE NULL END
FROM app.tasks AS task
WHERE task.id=admission.task_id AND task.workspace_id=admission.workspace_id;

CREATE UNIQUE INDEX guest_trial_task_admissions_task_id
 ON business.guest_trial_task_admissions(task_id);
CREATE INDEX guest_trial_task_admissions_waiting
 ON business.guest_trial_task_admissions(enqueued_at,task_id)
 WHERE queue_state='waiting';

CREATE FUNCTION business.sync_guest_trial_queue_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 UPDATE business.guest_trial_task_admissions
 SET queue_state = CASE NEW.state
       WHEN 'queued' THEN 'waiting'
       WHEN 'running' THEN 'running'
       WHEN 'succeeded' THEN 'succeeded'
       WHEN 'failed' THEN 'failed'
       WHEN 'cancelled' THEN 'cancelled'
       ELSE 'paused'
     END,
     enqueued_at = CASE
       WHEN NEW.state='queued' AND OLD.state IS DISTINCT FROM 'queued'
         THEN clock_timestamp()
       ELSE enqueued_at
     END,
     last_claimed_at = CASE
       WHEN NEW.state='running' THEN clock_timestamp()
       ELSE last_claimed_at
     END
 WHERE task_id=NEW.id AND workspace_id=NEW.workspace_id;
 RETURN NEW;
END;
$$;

CREATE TRIGGER guest_trial_queue_state_sync
 AFTER UPDATE OF state,lease_generation ON app.tasks
 FOR EACH ROW
 WHEN (OLD.state IS DISTINCT FROM NEW.state
   OR OLD.lease_generation IS DISTINCT FROM NEW.lease_generation)
 EXECUTE FUNCTION business.sync_guest_trial_queue_state();
