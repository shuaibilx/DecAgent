-- Preserve every real provider attempt and its settlement independently.
ALTER TABLE business.guest_trial_usage_reservations
 DROP CONSTRAINT guest_trial_usage_reservations_workspace_id_request_key_key;

ALTER TABLE business.guest_trial_usage_reservations
 ADD COLUMN attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
 ADD COLUMN usage_record_id uuid,
 ADD COLUMN dispatched_at timestamptz,
 ADD COLUMN settled_at timestamptz,
 ADD COLUMN actual_input_tokens bigint CHECK (actual_input_tokens >= 0),
 ADD COLUMN actual_output_tokens bigint CHECK (actual_output_tokens >= 0),
 ADD COLUMN actual_cost_native numeric(20,12) CHECK (actual_cost_native >= 0),
 ADD COLUMN actual_cost_usd numeric(18,6) CHECK (actual_cost_usd >= 0),
 ADD COLUMN actual_cost_basis text CHECK (
  actual_cost_basis IS NULL OR actual_cost_basis IN ('provider_billed','frozen_rate')
 ),
 ADD COLUMN voucher_credit_native numeric(20,12) CHECK (voucher_credit_native >= 0),
 ADD COLUMN cash_paid_native numeric(20,12) CHECK (cash_paid_native >= 0),
 ADD COLUMN billing_evidence_source text CHECK (
  billing_evidence_source IS NULL OR billing_evidence_source IN ('provider_response','invoice')
 ),
 ADD COLUMN reservation_exceeded boolean NOT NULL DEFAULT false,
 ADD COLUMN not_sent_reason text CHECK (
  not_sent_reason IS NULL OR length(not_sent_reason) BETWEEN 1 AND 255
 ),
 ADD COLUMN server_request_id text CHECK (
  server_request_id IS NULL OR length(server_request_id) BETWEEN 1 AND 255
 ),
 ADD COLUMN latency_ms integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
 ADD COLUMN error_category text CHECK (
  error_category IS NULL OR error_category IN (
   'network_error','timeout','rate_limited','provider_error','response_invalid',
   'usage_unknown','unknown_error'
  )
 ),
 DROP CONSTRAINT guest_trial_usage_reservations_status_check,
 ADD CONSTRAINT guest_trial_usage_reservations_status_check
  CHECK (status IN ('reserved','dispatched','settled','unknown','not_sent')),
 ADD CONSTRAINT guest_trial_usage_reservations_settled_check
  CHECK (
   status <> 'settled'
   OR (actual_input_tokens IS NOT NULL AND actual_output_tokens IS NOT NULL
       AND actual_cost_native IS NOT NULL AND actual_cost_usd IS NOT NULL
       AND actual_cost_basis IS NOT NULL AND settled_at IS NOT NULL)
  );

CREATE UNIQUE INDEX guest_trial_usage_reservations_workspace_attempt
 ON business.guest_trial_usage_reservations(workspace_id,request_key,attempt);
CREATE UNIQUE INDEX guest_trial_usage_reservations_usage_record
 ON business.guest_trial_usage_reservations(usage_record_id)
 WHERE usage_record_id IS NOT NULL;
