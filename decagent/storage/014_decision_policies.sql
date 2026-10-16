-- Empty by default: report import and policy publication require trusted administration.
CREATE TABLE app.decision_validation_reports (
 report_id text PRIMARY KEY CHECK(length(report_id) BETWEEN 1 AND 200),
 report_digest text NOT NULL CHECK(report_digest ~ '^[a-f0-9]{64}$'),
 report jsonb NOT NULL CHECK(jsonb_typeof(report)='object' AND octet_length(report::text)<=262144),
 registered_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE app.decision_policies (
 policy_id text NOT NULL CHECK(length(policy_id) BETWEEN 1 AND 200),
 policy_version text NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 200),
 definition jsonb NOT NULL
   CHECK(jsonb_typeof(definition)='object' AND octet_length(definition::text)<=65536),
 definition_digest text NOT NULL CHECK(definition_digest ~ '^[a-f0-9]{64}$'),
 validation_record text NOT NULL REFERENCES app.decision_validation_reports(report_id),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','revoked')),
 minimum_threshold double precision NOT NULL CHECK(minimum_threshold>=0 AND minimum_threshold<=1),
 registered_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 published_at timestamptz,
 revoked_at timestamptz,
 PRIMARY KEY(policy_id,policy_version)
);

CREATE TABLE app.task_decision_thresholds (
 task_id uuid NOT NULL REFERENCES app.tasks(id),
 policy_id text NOT NULL,
 policy_version text NOT NULL,
 threshold double precision NOT NULL CHECK(threshold>=0 AND threshold<=1),
 revision bigint NOT NULL CHECK(revision>0),
 changed_by uuid NOT NULL REFERENCES business.employee_accounts(id),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(task_id,policy_id,policy_version),
 FOREIGN KEY(policy_id,policy_version) REFERENCES app.decision_policies(policy_id,policy_version)
);

CREATE TABLE app.task_decision_policy_bindings (
 task_id uuid NOT NULL REFERENCES app.tasks(id),
 policy_id text NOT NULL,
 policy_version text NOT NULL,
 effective_revision bigint NOT NULL CHECK(effective_revision>0),
 effective_digest text NOT NULL CHECK(effective_digest ~ '^[a-f0-9]{64}$'),
 configuration jsonb NOT NULL
   CHECK(jsonb_typeof(configuration)='object' AND octet_length(configuration::text)<=65536),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(task_id,policy_id,policy_version),
 FOREIGN KEY(policy_id,policy_version) REFERENCES app.decision_policies(policy_id,policy_version)
);

CREATE TABLE app.decision_policy_events (
 id bigserial PRIMARY KEY,
 policy_id text,
 policy_version text,
 task_id uuid REFERENCES app.tasks(id),
 kind text NOT NULL CHECK(length(kind) BETWEEN 1 AND 100),
 actor_kind text NOT NULL CHECK(actor_kind IN ('owner','employee')),
 actor_id text NOT NULL,
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((policy_id IS NULL)=(policy_version IS NULL)),
 FOREIGN KEY(policy_id,policy_version) REFERENCES app.decision_policies(policy_id,policy_version)
);
CREATE INDEX decision_policy_events_task ON app.decision_policy_events(task_id,id);

CREATE FUNCTION app.keep_decision_policy_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Decision policy evidence and history are immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER decision_report_immutable BEFORE UPDATE OR DELETE
 ON app.decision_validation_reports FOR EACH ROW EXECUTE FUNCTION app.keep_decision_policy_evidence();
CREATE TRIGGER decision_policy_event_immutable BEFORE UPDATE OR DELETE
 ON app.decision_policy_events FOR EACH ROW EXECUTE FUNCTION app.keep_decision_policy_evidence();

CREATE FUNCTION app.guard_decision_policy_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'Decision policy versions are retained' USING ERRCODE='23514';
 END IF;
 IF (NEW.policy_id,NEW.policy_version,NEW.definition,NEW.definition_digest,
     NEW.validation_record,NEW.registered_by,NEW.created_at)
   IS DISTINCT FROM
    (OLD.policy_id,OLD.policy_version,OLD.definition,OLD.definition_digest,
     OLD.validation_record,OLD.registered_by,OLD.created_at) THEN
   RAISE EXCEPTION 'Decision policy definitions are immutable' USING ERRCODE='23514';
 END IF;
 IF (OLD.status='revoked' AND NEW.status<>'revoked')
   OR (OLD.status='published' AND NEW.status='draft')
   OR NEW.minimum_threshold<OLD.minimum_threshold THEN
   RAISE EXCEPTION 'Policy revocation and minimum are monotone' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER decision_policy_version_guard BEFORE UPDATE OR DELETE
 ON app.decision_policies FOR EACH ROW EXECUTE FUNCTION app.guard_decision_policy_version();
