-- Semantic decisions are separate from concrete action/write confirmations.
ALTER TABLE app.approvals DROP CONSTRAINT approvals_kind_check;
ALTER TABLE app.approvals ADD CONSTRAINT approvals_kind_check CHECK(kind IN
 ('action','code_trust','acceptance','memory_conflict','clarification','decision'));

CREATE TABLE app.business_decision_interventions (
 approval_id uuid PRIMARY KEY REFERENCES app.approvals(id),
 task_id uuid NOT NULL REFERENCES app.tasks(id),
 actor_employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 node_id text NOT NULL,
 execution_generation integer NOT NULL CHECK(execution_generation>0),
 plan_revision integer NOT NULL CHECK(plan_revision BETWEEN 1 AND 4),
 plan_digest text NOT NULL CHECK(plan_digest ~ '^[a-f0-9]{64}$'),
 binding_digest text NOT NULL CHECK(binding_digest ~ '^[a-f0-9]{64}$'),
 binding jsonb NOT NULL
   CHECK(jsonb_typeof(binding)='object' AND octet_length(binding::text)<=262144),
 gate_snapshot jsonb NOT NULL
   CHECK(jsonb_typeof(gate_snapshot)='object' AND octet_length(gate_snapshot::text)<=262144),
 status text NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','replied','consumed','stale','expired')),
 expires_at timestamptz NOT NULL,
 response_fingerprint text CHECK(response_fingerprint ~ '^[a-f0-9]{64}$'),
 response_record jsonb CHECK(octet_length(response_record::text)<=131072),
 consumed_by text,
 consumed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(task_id,node_id,execution_generation,binding_digest),
 FOREIGN KEY(task_id,node_id,execution_generation)
   REFERENCES app.business_plan_nodes(task_id,node_id,execution_generation),
 FOREIGN KEY(task_id,plan_revision,plan_digest)
   REFERENCES app.business_plan_revisions(task_id,revision,content_digest),
 CHECK(status NOT IN ('replied','consumed') OR
   (response_fingerprint IS NOT NULL AND response_record IS NOT NULL)),
 CHECK(status<>'consumed' OR (consumed_by IS NOT NULL AND consumed_at IS NOT NULL))
);
CREATE INDEX business_decision_intervention_task
 ON app.business_decision_interventions(task_id,status);

CREATE TABLE app.business_optional_intervention_rules (
 rule_id text NOT NULL,
 rule_version text NOT NULL,
 question_id text NOT NULL CHECK(question_id IN ('memory_read_needed','memory_relevant')),
 question_version text NOT NULL,
 primitive text NOT NULL CHECK(primitive IN ('choice','noul','score')),
 policy_id text NOT NULL,
 policy_version text NOT NULL,
 purpose text NOT NULL CHECK(purpose='optional_memory'),
 low_score_action text NOT NULL CHECK(low_score_action='skip'),
 context jsonb NOT NULL CHECK(jsonb_typeof(context)='object' AND octet_length(context::text)<=16384),
 reference text NOT NULL CHECK(length(reference) BETWEEN 1 AND 512),
 status text NOT NULL CHECK(status IN ('published','revoked')),
 valid_until timestamptz NOT NULL,
 published_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(rule_id,rule_version),
 FOREIGN KEY(policy_id,policy_version) REFERENCES app.decision_policies(policy_id,policy_version)
);

CREATE FUNCTION app.guard_semantic_intervention() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'Semantic decision evidence is retained' USING ERRCODE='23514';
 END IF;
 IF (NEW.approval_id,NEW.task_id,NEW.actor_employee_id,NEW.node_id,NEW.execution_generation,
     NEW.plan_revision,NEW.plan_digest,NEW.binding_digest,NEW.binding,NEW.gate_snapshot,
     NEW.expires_at,NEW.created_at)
   IS DISTINCT FROM
    (OLD.approval_id,OLD.task_id,OLD.actor_employee_id,OLD.node_id,OLD.execution_generation,
     OLD.plan_revision,OLD.plan_digest,OLD.binding_digest,OLD.binding,OLD.gate_snapshot,
     OLD.expires_at,OLD.created_at) THEN
   RAISE EXCEPTION 'Semantic decision binding is immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.status IN ('consumed','stale','expired') AND NEW IS DISTINCT FROM OLD THEN
   RAISE EXCEPTION 'Terminal semantic decision history is immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.response_record IS NOT NULL AND
   (NEW.response_record,NEW.response_fingerprint) IS DISTINCT FROM
   (OLD.response_record,OLD.response_fingerprint) THEN
   RAISE EXCEPTION 'Authenticated semantic reply is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER semantic_intervention_guard BEFORE UPDATE OR DELETE
 ON app.business_decision_interventions FOR EACH ROW EXECUTE FUNCTION app.guard_semantic_intervention();

CREATE FUNCTION app.guard_optional_intervention_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'Optional rule history is retained' USING ERRCODE='23514';
 END IF;
 IF (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status')
    OR (OLD.status='revoked' AND NEW.status<>'revoked') THEN
   RAISE EXCEPTION 'Optional rule definitions and revocations are immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER optional_intervention_rule_guard BEFORE UPDATE OR DELETE
 ON app.business_optional_intervention_rules FOR EACH ROW EXECUTE FUNCTION app.guard_optional_intervention_rule();
