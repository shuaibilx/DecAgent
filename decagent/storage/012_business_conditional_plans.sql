-- Append conditional-plan state; preserve every legacy checkpoint and operation.
CREATE TABLE app.business_plan_revisions (
 task_id uuid NOT NULL REFERENCES app.tasks(id),
 revision integer NOT NULL CHECK(revision BETWEEN 1 AND 4),
 plan_id text NOT NULL,
 schema_version text NOT NULL CHECK(schema_version='1'),
 content_digest text NOT NULL CHECK(content_digest ~ '^[a-f0-9]{64}$'),
 registry_fingerprint text NOT NULL CHECK(registry_fingerprint ~ '^[a-f0-9]{64}$'),
 parent_revision integer, parent_digest text,
 definition jsonb NOT NULL CHECK(jsonb_typeof(definition)='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(task_id,revision),
 UNIQUE(task_id,content_digest),
 UNIQUE(task_id,revision,content_digest),
 FOREIGN KEY(task_id,parent_revision,parent_digest)
   REFERENCES app.business_plan_revisions(task_id,revision,content_digest),
 CHECK((revision=1 AND parent_revision IS NULL AND parent_digest IS NULL)
    OR (revision>1 AND parent_revision IS NOT NULL AND parent_revision=revision-1
        AND parent_digest IS NOT NULL
        AND parent_digest ~ '^[a-f0-9]{64}$')),
 CHECK(octet_length(definition::text)<=262144)
);

CREATE TABLE app.business_plan_heads (
 task_id uuid PRIMARY KEY REFERENCES app.tasks(id),
 current_revision integer NOT NULL,
 node_attempts integer NOT NULL DEFAULT 0 CHECK(node_attempts>=0),
 graph_transitions integer NOT NULL DEFAULT 0 CHECK(graph_transitions>=0),
 context_snapshot jsonb NOT NULL DEFAULT '{}'
   CHECK(jsonb_typeof(context_snapshot)='object'
     AND octet_length(context_snapshot::text)<=262144),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(task_id,current_revision) REFERENCES app.business_plan_revisions(task_id,revision)
);

CREATE TABLE app.business_plan_nodes (
 task_id uuid NOT NULL REFERENCES app.tasks(id),
 node_id text NOT NULL CHECK(node_id ~ '^[a-z][a-z0-9_-]{0,63}$'),
 execution_generation integer NOT NULL DEFAULT 1 CHECK(execution_generation>0),
 plan_revision integer NOT NULL,
 node_digest text NOT NULL CHECK(node_digest ~ '^[a-f0-9]{64}$'),
 status text NOT NULL CHECK(status IN
   ('running','succeeded','skipped','waiting','failed','unknown')),
 input_digest text NOT NULL CHECK(input_digest ~ '^[a-f0-9]{64}$'),
 input_snapshot jsonb NOT NULL
   CHECK(jsonb_typeof(input_snapshot)='object' AND octet_length(input_snapshot::text)<=262144),
 output_snapshot jsonb CHECK(octet_length(output_snapshot::text)<=524288),
 action_id uuid,
 dispatch_attempts integer NOT NULL DEFAULT 0 CHECK(dispatch_attempts>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(task_id,node_id,execution_generation),
 UNIQUE(action_id),
 FOREIGN KEY(task_id,plan_revision) REFERENCES app.business_plan_revisions(task_id,revision),
 FOREIGN KEY(action_id,task_id) REFERENCES app.actions(id,task_id),
 CHECK(status<>'succeeded' OR output_snapshot IS NOT NULL)
);
CREATE INDEX business_plan_node_status ON app.business_plan_nodes(task_id,status);

CREATE FUNCTION app.keep_business_plan_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Published plan revisions are immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER business_plan_revision_immutable BEFORE UPDATE OR DELETE
 ON app.business_plan_revisions FOR EACH ROW EXECUTE FUNCTION app.keep_business_plan_revision();

