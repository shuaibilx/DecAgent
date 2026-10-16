-- Employee identity is separate from the owner used for operations and repair.
-- Every new actor column is nullable so historical v1 rows retain unknown identity.
CREATE TABLE business.employee_accounts (
 id uuid PRIMARY KEY,
 login text NOT NULL UNIQUE CHECK (login ~ '^[a-z][a-z0-9-]{0,63}$'),
 person_id uuid NOT NULL UNIQUE REFERENCES business.people(id),
 role text NOT NULL CHECK (role IN ('employee','handler','supervisor')),
 password_hash text NOT NULL CHECK (length(password_hash) BETWEEN 20 AND 512),
 active boolean NOT NULL DEFAULT true,
 credential_revision bigint NOT NULL DEFAULT 1 CHECK (credential_revision > 0),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE business.employee_sessions (
 token_hash text PRIMARY KEY CHECK (token_hash ~ '^[a-f0-9]{64}$'),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 account_revision bigint NOT NULL CHECK (account_revision > 0),
 csrf_hash text NOT NULL CHECK (csrf_hash ~ '^[a-f0-9]{64}$'),
 expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX employee_sessions_actor ON business.employee_sessions(employee_id,expires_at);

CREATE TABLE business.mcp_credentials (
 id uuid PRIMARY KEY,
 token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 account_revision bigint NOT NULL CHECK (account_revision > 0),
 expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 last_used_at timestamptz,
 UNIQUE(id,employee_id)
);
CREATE INDEX mcp_credentials_actor ON business.mcp_credentials(employee_id,expires_at);

ALTER TABLE app.tasks ADD COLUMN employee_id uuid
 REFERENCES business.employee_accounts(id);
ALTER TABLE app.tasks ADD COLUMN credential_ref uuid
 REFERENCES business.mcp_credentials(id);
ALTER TABLE app.tasks ADD CONSTRAINT tasks_credential_actor_check
 CHECK (credential_ref IS NULL OR employee_id IS NOT NULL);
ALTER TABLE app.tasks ADD CONSTRAINT tasks_credential_actor_fk
 FOREIGN KEY (credential_ref,employee_id)
 REFERENCES business.mcp_credentials(id,employee_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.tickets ADD COLUMN employee_id uuid
 REFERENCES business.employee_accounts(id);
ALTER TABLE business.ticket_history ADD COLUMN employee_id uuid
 REFERENCES business.employee_accounts(id);
ALTER TABLE business.ticket_history ADD COLUMN operation_id text;
ALTER TABLE business.ticket_history ADD COLUMN source text
 CHECK (source IS NULL OR source IN ('external_mcp','decagent'));
ALTER TABLE business.ticket_history ADD CONSTRAINT ticket_history_actor_check
 CHECK (employee_id IS NULL OR (operation_id IS NOT NULL AND source IS NOT NULL));
ALTER TABLE business.operation_receipts ADD COLUMN employee_id uuid
 REFERENCES business.employee_accounts(id);
ALTER TABLE business.operation_receipts ADD COLUMN contract_digest text
 CHECK (contract_digest IS NULL OR contract_digest ~ '^[a-f0-9]{64}$');
ALTER TABLE business.operation_receipts ADD CONSTRAINT operation_receipts_actor_check
 CHECK ((employee_id IS NULL AND contract_digest IS NULL)
        OR (employee_id IS NOT NULL AND contract_digest IS NOT NULL));

CREATE TABLE business.employee_audit (
 id uuid PRIMARY KEY,
 actor_employee_id uuid REFERENCES business.employee_accounts(id),
 source text NOT NULL CHECK (source IN ('web','mcp','agent','admin','system')),
 event text NOT NULL CHECK (length(event) BETWEEN 1 AND 100),
 outcome text NOT NULL CHECK (outcome IN ('allowed','denied','failed','observed')),
 ticket_id uuid REFERENCES business.tickets(id),
 operation_id text,
 task_id uuid REFERENCES app.tasks(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX employee_audit_actor_time ON business.employee_audit(actor_employee_id,created_at);
