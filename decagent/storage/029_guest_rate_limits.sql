-- Persist only keyed client-address digests. Raw network addresses are never stored.
CREATE TABLE business.guest_rate_limit_keys (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 secret bytea NOT NULL CHECK (octet_length(secret)=32),
 created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE business.guest_rate_limit_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 scope text NOT NULL CHECK (scope IN (
  'guest_create','guest_task_submit','guest_manual_reply'
  )),
 subject_hash bytea NOT NULL CHECK (octet_length(subject_hash)=32),
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX guest_rate_limit_events_subject_window
 ON business.guest_rate_limit_events(scope,subject_hash,occurred_at);
CREATE INDEX guest_rate_limit_events_retention
 ON business.guest_rate_limit_events(occurred_at);
