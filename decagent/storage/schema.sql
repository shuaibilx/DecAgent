CREATE TABLE app.projects (
 id uuid PRIMARY KEY, repository text NOT NULL UNIQUE,
 base_branch text NOT NULL DEFAULT 'main', config_version integer NOT NULL DEFAULT 1,
 credential_ref text NOT NULL DEFAULT 'github', trust jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.tasks (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES app.projects(id),
 base_sha text NOT NULL CHECK (base_sha ~ '^[a-f0-9]{40}$'),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
 ('queued','running','awaiting_input','awaiting_approval','paused_budget','recovering',
 'succeeded','failed','cancelled')),
 phase text NOT NULL DEFAULT 'prepare' CHECK(phase IN
 ('prepare','reproduce','repair','verify','deliver','finalize')),
 authorization_snapshot jsonb NOT NULL, budget jsonb NOT NULL DEFAULT '{}',
 graph_version text NOT NULL DEFAULT '1', lease_owner text,
 lease_generation bigint NOT NULL DEFAULT 0, lease_until timestamptz,
 event_seq bigint NOT NULL DEFAULT 0, active_seconds numeric NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,project_id)
);
CREATE TABLE app.events (
 task_id uuid NOT NULL, project_id uuid NOT NULL, seq bigint NOT NULL,
 kind text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(task_id,seq), FOREIGN KEY(task_id,project_id)
 REFERENCES app.tasks(id,project_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE app.actions (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES app.tasks(id),
 logical_key text NOT NULL, digest text NOT NULL, snapshot jsonb NOT NULL,
 status text NOT NULL DEFAULT 'planned' CHECK(status IN
 ('planned','running','succeeded','failed','unknown','denied')),
 receipt jsonb, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(task_id,logical_key), UNIQUE(id,task_id)
);
CREATE TABLE app.action_attempts (
 id uuid PRIMARY KEY, action_id uuid NOT NULL REFERENCES app.actions(id),
 generation bigint NOT NULL, execution_id text NOT NULL UNIQUE,
 status text NOT NULL, receipt jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.approvals (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES app.tasks(id), action_id uuid,
 digest text NOT NULL, kind text NOT NULL CHECK(kind IN
 ('action','code_trust','acceptance','memory_conflict','clarification')),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','replied','consumed')),
 reply text CHECK(reply IN ('approve','reject','edit','confirm')),
 reply_payload jsonb, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), replied_at timestamptz,
 FOREIGN KEY(action_id,task_id) REFERENCES app.actions(id,task_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(task_id,digest,kind)
);
CREATE TABLE app.evidence (
 sha256 text PRIMARY KEY CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 size bigint NOT NULL CHECK(size>=0), source text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.verification_runs (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES app.tasks(id),
 kind text NOT NULL CHECK(kind IN ('baseline','independent','regression')),
 base_sha text NOT NULL, tree_hash text NOT NULL, checker_hash text NOT NULL,
 command jsonb NOT NULL, environment jsonb NOT NULL, test_count integer NOT NULL,
 status text NOT NULL CHECK(status IN ('passed','failed','environment_error','unverified')),
 evidence_hash text REFERENCES app.evidence(sha256), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.decision_records (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES app.tasks(id),
 input_hash text NOT NULL, question_version text NOT NULL, policy_version text NOT NULL,
 requested_model text NOT NULL, resolved_model text, result jsonb NOT NULL,
 final_route text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.usage_records (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES app.tasks(id),
 reservation_key text NOT NULL, attempt integer NOT NULL,
 provider text NOT NULL, requested_tokens bigint NOT NULL CHECK(requested_tokens>=0),
 reserved_cost numeric CHECK(reserved_cost>=0), actual_tokens bigint CHECK(actual_tokens>=0),
 actual_cost numeric CHECK(actual_cost>=0), status text NOT NULL DEFAULT 'reserved'
 CHECK(status IN ('reserved','unknown','settled')), created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(task_id,reservation_key,attempt)
);
CREATE TABLE app.memory_candidates (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES app.projects(id),
 source_task_id uuid NOT NULL, fingerprint text NOT NULL, category text NOT NULL
 CHECK(category IN ('fact','design','cause')), content text NOT NULL,
 source_pr text, source_blob text, evidence_hash text REFERENCES app.evidence(sha256),
 user_confirmed boolean NOT NULL DEFAULT false, applicability jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'task_local' CHECK(status IN
 ('task_local','pending_review','pending_merge','active','rejected','revoked','superseded')),
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(source_task_id,project_id) REFERENCES app.tasks(id,project_id)
 DEFERRABLE INITIALLY DEFERRED, UNIQUE(project_id,fingerprint), UNIQUE(id,project_id)
);
CREATE TABLE app.memory_versions (
 id uuid PRIMARY KEY, candidate_id uuid NOT NULL, project_id uuid NOT NULL,
 version integer NOT NULL, content text NOT NULL, confirmed_by text, merge_sha text,
 source_blob text, valid_from timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
 revoked_at timestamptz, replaces uuid REFERENCES app.memory_versions(id),
 FOREIGN KEY(candidate_id,project_id) REFERENCES app.memory_candidates(id,project_id)
 DEFERRABLE INITIALLY DEFERRED, UNIQUE(candidate_id,version)
);
CREATE INDEX memory_search ON app.memory_versions
 USING gin(to_tsvector('simple',content));
CREATE TABLE app.deliveries (
 id uuid PRIMARY KEY, task_id uuid NOT NULL UNIQUE REFERENCES app.tasks(id),
 branch text NOT NULL UNIQUE, head_sha text NOT NULL, tree_hash text NOT NULL,
 status text NOT NULL, pr_url text UNIQUE, pr_number integer, merge_sha text,
 merged_content jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.owner_sessions (
 token_hash text PRIMARY KEY, expires_at timestamptz NOT NULL, csrf_hash text NOT NULL,
 revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.owner_credentials (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), token_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.idempotency_keys (
 scope text NOT NULL, key text NOT NULL, input_hash text NOT NULL,
 response jsonb, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(scope,key)
);
