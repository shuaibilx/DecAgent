-- Guest browser credentials are separate from employee login credentials and stay
-- bound to a single simulated workspace and one active employee identity.
CREATE TABLE business.guest_sessions (
 token_hash text PRIMARY KEY CHECK (token_hash ~ '^[a-f0-9]{64}$'),
 workspace_id uuid NOT NULL UNIQUE REFERENCES business.workspaces(id),
 employee_id uuid NOT NULL,
 account_revision bigint NOT NULL CHECK (account_revision > 0),
 csrf_hash text NOT NULL CHECK (csrf_hash ~ '^[a-f0-9]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 FOREIGN KEY (employee_id,workspace_id)
  REFERENCES business.employee_accounts(id,workspace_id)
);
CREATE INDEX guest_sessions_expiry ON business.guest_sessions(expires_at)
 WHERE revoked_at IS NULL;
