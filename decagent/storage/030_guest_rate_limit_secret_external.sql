-- The key must remain outside PostgreSQL so a database snapshot cannot expose it.
CREATE TABLE business.guest_rate_limit_key_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 key_fingerprint bytea NOT NULL CHECK (octet_length(key_fingerprint)=32),
 activated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

DROP TABLE IF EXISTS business.guest_rate_limit_keys;
