ALTER TABLE app.deliveries ADD COLUMN base_sha text;
ALTER TABLE app.deliveries ADD COLUMN intent jsonb NOT NULL DEFAULT '{}';
ALTER TABLE app.deliveries ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
