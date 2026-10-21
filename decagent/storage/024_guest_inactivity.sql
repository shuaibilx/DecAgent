-- Guest sessions expire after seven days without an authenticated user request.
ALTER TABLE business.guest_sessions
 ADD COLUMN last_activity_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX guest_sessions_idle_expiry
 ON business.guest_sessions(last_activity_at)
 WHERE revoked_at IS NULL;
