-- Empty by default. Successful tasks never publish reusable workflow versions.
CREATE TABLE app.workflow_validation_reports (
 report_id text PRIMARY KEY CHECK(length(report_id) BETWEEN 1 AND 200),
 report_digest text NOT NULL CHECK(report_digest ~ '^[a-f0-9]{64}$'),
 definition_digest text NOT NULL CHECK(definition_digest ~ '^[a-f0-9]{64}$'),
 report jsonb NOT NULL
   CHECK(jsonb_typeof(report)='object' AND octet_length(report::text)<=262144),
 registered_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE app.business_workflows (
 workflow_id text NOT NULL CHECK(workflow_id ~ '^[a-z][a-z0-9_-]{0,63}$'),
 version text NOT NULL CHECK(length(version) BETWEEN 1 AND 200),
 goal_id text NOT NULL CHECK(goal_id ~ '^[a-z][a-z0-9_-]{0,63}$'),
 scope_id uuid NOT NULL REFERENCES app.business_scopes(id),
 scope_revision bigint NOT NULL CHECK(scope_revision>0),
 definition jsonb NOT NULL
   CHECK(jsonb_typeof(definition)='object' AND octet_length(definition::text)<=262144),
 definition_digest text NOT NULL CHECK(definition_digest ~ '^[a-f0-9]{64}$'),
 status text NOT NULL DEFAULT 'draft'
   CHECK(status IN ('draft','validated','published','revoked')),
 validation_report_id text REFERENCES app.workflow_validation_reports(report_id),
 registered_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 validated_at timestamptz,
 published_at timestamptz,
 revoked_at timestamptz,
 PRIMARY KEY(workflow_id,version),
 UNIQUE(workflow_id,version,definition_digest),
 CHECK(status NOT IN ('validated','published') OR validation_report_id IS NOT NULL),
 CHECK(status<>'published' OR published_at IS NOT NULL)
);
CREATE INDEX business_workflow_matching ON app.business_workflows(scope_id,goal_id,status);

CREATE TABLE app.workflow_instances (
 task_id uuid PRIMARY KEY REFERENCES app.tasks(id),
 instance_id uuid NOT NULL UNIQUE,
 workflow_id text NOT NULL,
 version text NOT NULL,
 definition_digest text NOT NULL,
 report_digest text NOT NULL CHECK(report_digest ~ '^[a-f0-9]{64}$'),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 scope_revision bigint NOT NULL CHECK(scope_revision>0),
 account_revision bigint NOT NULL CHECK(account_revision>0),
 input_digest text NOT NULL CHECK(input_digest ~ '^[a-f0-9]{64}$'),
 task_input jsonb NOT NULL
   CHECK(jsonb_typeof(task_input)='object' AND octet_length(task_input::text)<=131072),
 plan jsonb NOT NULL CHECK(jsonb_typeof(plan)='object' AND octet_length(plan::text)<=262144),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(workflow_id,version,definition_digest)
   REFERENCES app.business_workflows(workflow_id,version,definition_digest)
);

CREATE TABLE app.workflow_events (
 id bigserial PRIMARY KEY,
 workflow_id text NOT NULL,
 version text NOT NULL,
 task_id uuid REFERENCES app.tasks(id),
 kind text NOT NULL CHECK(length(kind) BETWEEN 1 AND 100),
 actor_kind text NOT NULL CHECK(actor_kind IN ('owner','employee')),
 actor_id text NOT NULL,
 payload jsonb NOT NULL
   CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(workflow_id,version) REFERENCES app.business_workflows(workflow_id,version)
);
CREATE INDEX workflow_events_task ON app.workflow_events(task_id,id);

CREATE FUNCTION app.keep_workflow_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Workflow reports, instances and history are immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER workflow_report_immutable BEFORE UPDATE OR DELETE
 ON app.workflow_validation_reports FOR EACH ROW EXECUTE FUNCTION app.keep_workflow_evidence();
CREATE TRIGGER workflow_instance_immutable BEFORE UPDATE OR DELETE
 ON app.workflow_instances FOR EACH ROW EXECUTE FUNCTION app.keep_workflow_evidence();
CREATE TRIGGER workflow_event_immutable BEFORE UPDATE OR DELETE
 ON app.workflow_events FOR EACH ROW EXECUTE FUNCTION app.keep_workflow_evidence();

CREATE FUNCTION app.guard_workflow_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'Workflow versions are retained' USING ERRCODE='23514';
 END IF;
 IF (NEW.workflow_id,NEW.version,NEW.goal_id,NEW.scope_id,NEW.scope_revision,
     NEW.definition,NEW.definition_digest,NEW.registered_by,NEW.created_at)
   IS DISTINCT FROM
    (OLD.workflow_id,OLD.version,OLD.goal_id,OLD.scope_id,OLD.scope_revision,
     OLD.definition,OLD.definition_digest,OLD.registered_by,OLD.created_at) THEN
   RAISE EXCEPTION 'Workflow definitions are immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.status<>OLD.status AND NOT (
      (OLD.status='draft' AND NEW.status IN ('validated','revoked'))
   OR (OLD.status='validated' AND NEW.status IN ('published','revoked'))
   OR (OLD.status='published' AND NEW.status='revoked')) THEN
   RAISE EXCEPTION 'Workflow publication requires validated monotone transitions'
     USING ERRCODE='23514';
 END IF;
 IF OLD.validation_report_id IS NOT NULL AND
    NEW.validation_report_id IS DISTINCT FROM OLD.validation_report_id THEN
   RAISE EXCEPTION 'Workflow validation binding is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.validation_report_id IS DISTINCT FROM OLD.validation_report_id
    AND NOT (OLD.status='draft' AND NEW.status='validated') THEN
   RAISE EXCEPTION 'Workflow reports bind only at validation' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER workflow_version_guard BEFORE UPDATE OR DELETE
 ON app.business_workflows FOR EACH ROW EXECUTE FUNCTION app.guard_workflow_version();
