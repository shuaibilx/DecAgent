ALTER TABLE app.usage_records
 ADD COLUMN IF NOT EXISTS provider_status integer CHECK(
   provider_status IS NULL OR provider_status BETWEEN 100 AND 599),
 ADD COLUMN IF NOT EXISTS requested_output_tokens integer CHECK(
   requested_output_tokens IS NULL OR requested_output_tokens BETWEEN 1 AND 8192);
