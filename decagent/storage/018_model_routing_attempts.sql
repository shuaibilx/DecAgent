ALTER TABLE app.usage_records
 ADD COLUMN IF NOT EXISTS request_role text CHECK(
   request_role IS NULL OR length(request_role) BETWEEN 1 AND 64),
 ADD COLUMN IF NOT EXISTS requested_model text CHECK(
   requested_model IS NULL OR length(requested_model) BETWEEN 1 AND 255),
 ADD COLUMN IF NOT EXISTS resolved_model text CHECK(
   resolved_model IS NULL OR length(resolved_model) BETWEEN 1 AND 255),
 ADD COLUMN IF NOT EXISTS endpoint text CHECK(
   endpoint IS NULL OR length(endpoint) BETWEEN 1 AND 512),
 ADD COLUMN IF NOT EXISTS server_request_id text CHECK(
   server_request_id IS NULL OR length(server_request_id) BETWEEN 1 AND 255),
 ADD COLUMN IF NOT EXISTS latency_ms integer CHECK(latency_ms IS NULL OR latency_ms>=0),
 ADD COLUMN IF NOT EXISTS error_category text CHECK(
   error_category IS NULL OR length(error_category) BETWEEN 1 AND 64),
 ADD COLUMN IF NOT EXISTS recovery_of uuid;

ALTER TABLE app.usage_records
 ADD CONSTRAINT usage_records_id_task_unique UNIQUE(id,task_id),
 ADD CONSTRAINT usage_records_recovery_same_task_fk
   FOREIGN KEY(recovery_of,task_id) REFERENCES app.usage_records(id,task_id)
   ON DELETE RESTRICT,
 ADD CONSTRAINT usage_records_recovery_not_self CHECK(recovery_of IS NULL OR recovery_of<>id);

CREATE UNIQUE INDEX usage_records_single_recovery
 ON app.usage_records(recovery_of) WHERE recovery_of IS NOT NULL;
CREATE INDEX usage_records_provider_model
 ON app.usage_records(task_id,provider,requested_model,created_at);
