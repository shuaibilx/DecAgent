-- Unified service-owned operations for employee MCP and Agent dispatch.
CREATE TABLE business.operations (
 id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 200),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 200),
 scope_id uuid NOT NULL REFERENCES app.business_scopes(id),
 command text NOT NULL CHECK (command IN
 ('create','assign','start','comment','resolve','close','reopen','set_priority')),
 params jsonb NOT NULL, effective_params jsonb NOT NULL,
 input_hash text NOT NULL CHECK (input_hash ~ '^[a-f0-9]{64}$'),
 contract_digest text NOT NULL CHECK (contract_digest ~ '^[a-f0-9]{64}$'),
 expected_revision bigint CHECK (expected_revision>0), resource_snapshot jsonb,
 source text NOT NULL CHECK (source IN ('external_mcp','decagent')),
 state text NOT NULL CHECK (state IN
 ('ready','awaiting_approval','running','unknown','succeeded','failed','rejected','expired','cancelled')),
 confirmation_required boolean NOT NULL,
 agent_task_id uuid REFERENCES app.tasks(id), agent_action_id uuid,
 agent_generation bigint CHECK (agent_generation>0),
 supersedes text REFERENCES business.operations(id),
 attempt_id uuid, started_at timestamptz, error_code text,
 result jsonb, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(employee_id,request_id), UNIQUE(id,employee_id),
 FOREIGN KEY(agent_action_id,agent_task_id) REFERENCES app.actions(id,task_id),
 CHECK ((source='external_mcp' AND agent_task_id IS NULL AND agent_action_id IS NULL
         AND agent_generation IS NULL)
        OR (source='decagent' AND agent_task_id IS NOT NULL AND agent_action_id IS NOT NULL
            AND agent_generation IS NOT NULL)),
 CHECK ((state='succeeded' AND result IS NOT NULL) OR (state<>'succeeded' AND result IS NULL))
);
CREATE INDEX operations_actor_state ON business.operations(employee_id,state,created_at);

CREATE TABLE business.operation_approvals (
 operation_id text PRIMARY KEY,
 employee_id uuid NOT NULL,
 input_hash text NOT NULL CHECK (input_hash ~ '^[a-f0-9]{64}$'),
 contract_digest text NOT NULL CHECK (contract_digest ~ '^[a-f0-9]{64}$'),
 expected_revision bigint, policy_version text NOT NULL,
 decision text NOT NULL CHECK (decision IN ('approve','reject','edit')),
 approved_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 consumed_at timestamptz,
 FOREIGN KEY(operation_id,employee_id) REFERENCES business.operations(id,employee_id)
);

CREATE TABLE business.execution_grants (
 id uuid PRIMARY KEY,
 token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 task_id uuid NOT NULL REFERENCES app.tasks(id), action_id uuid NOT NULL,
 request_id text NOT NULL, input_hash text NOT NULL CHECK (input_hash ~ '^[a-f0-9]{64}$'),
 action_digest text NOT NULL, scope_id uuid NOT NULL REFERENCES app.business_scopes(id),
 contract_digest text NOT NULL CHECK (contract_digest ~ '^[a-f0-9]{64}$'),
 lease_owner text NOT NULL, generation bigint NOT NULL CHECK (generation>0),
 expires_at timestamptz NOT NULL, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(action_id,task_id) REFERENCES app.actions(id,task_id)
);
ALTER TABLE app.actions ADD COLUMN server_operation_id text REFERENCES business.operations(id);

CREATE FUNCTION business.keep_operation_input() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.id,NEW.employee_id,NEW.request_id,NEW.scope_id,NEW.command,NEW.params,
     NEW.effective_params,NEW.input_hash,NEW.contract_digest,NEW.expected_revision,
     NEW.resource_snapshot,NEW.source,NEW.confirmation_required,NEW.agent_task_id,
     NEW.agent_action_id,NEW.agent_generation,NEW.supersedes)
    IS DISTINCT FROM
    (OLD.id,OLD.employee_id,OLD.request_id,OLD.scope_id,OLD.command,OLD.params,
     OLD.effective_params,OLD.input_hash,OLD.contract_digest,OLD.expected_revision,
     OLD.resource_snapshot,OLD.source,OLD.confirmation_required,OLD.agent_task_id,
     OLD.agent_action_id,OLD.agent_generation,OLD.supersedes) THEN
  RAISE EXCEPTION 'Prepared operation inputs are immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER operation_input_immutable BEFORE UPDATE ON business.operations
 FOR EACH ROW EXECUTE FUNCTION business.keep_operation_input();
