-- Add auditable model attribution without guessing any historical classification.
ALTER TABLE app.usage_records
 ADD COLUMN IF NOT EXISTS model_family text CHECK(model_family IN ('primary','jev')),
 ADD COLUMN IF NOT EXISTS purpose text CHECK(length(purpose) BETWEEN 1 AND 64),
 ADD COLUMN IF NOT EXISTS plan_revision integer CHECK(plan_revision BETWEEN 1 AND 4),
 ADD COLUMN IF NOT EXISTS node_id text,
 ADD COLUMN IF NOT EXISTS node_ids jsonb CHECK(
   jsonb_typeof(node_ids)='array' AND jsonb_array_length(node_ids)<=32),
 ADD COLUMN IF NOT EXISTS question_count integer CHECK(question_count BETWEEN 1 AND 8),
 ADD COLUMN IF NOT EXISTS exception_reason text CHECK(length(exception_reason) BETWEEN 1 AND 64),
 ADD COLUMN IF NOT EXISTS dispatch_state text CHECK(
   dispatch_state IN ('reserved','dispatching','not_sent','responded','unknown')),
 ADD COLUMN IF NOT EXISTS error_code text CHECK(length(error_code) BETWEEN 1 AND 64),
 ADD COLUMN IF NOT EXISTS model_budget_version text;

CREATE INDEX IF NOT EXISTS usage_model_family
 ON app.usage_records(task_id,model_family,dispatch_state);

