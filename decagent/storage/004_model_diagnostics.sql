CREATE TABLE app.model_diagnostics (
 operation_id text PRIMARY KEY CHECK(operation_id ~ '^[a-f0-9]{64}$'),
 input_hash text NOT NULL CHECK(input_hash ~ '^[a-f0-9]{64}$'),
 target text NOT NULL CHECK(target IN ('main','jev')),
 requested_model text NOT NULL,
 status text NOT NULL CHECK(status IN ('dispatched','passed','failed','unknown')),
 reserved_cost numeric NOT NULL CHECK(reserved_cost>=0 AND reserved_cost<=0.05),
 actual_cost numeric CHECK(actual_cost>=0),
 result jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
