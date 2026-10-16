CREATE TABLE app.business_scopes (
 id uuid PRIMARY KEY,
 plugin_id text NOT NULL UNIQUE,
 plugin_version text NOT NULL,
 manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[a-f0-9]{64}$'),
 manifest jsonb NOT NULL,
 enabled boolean NOT NULL DEFAULT true,
 revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.tasks ADD COLUMN kind text NOT NULL DEFAULT 'repository_repair'
 CHECK (kind IN ('repository_repair','business'));
ALTER TABLE app.tasks ADD COLUMN business_scope_id uuid REFERENCES app.business_scopes(id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.tasks ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE app.tasks ALTER COLUMN base_sha DROP NOT NULL;
ALTER TABLE app.tasks ADD CONSTRAINT tasks_scope_check CHECK (
 (kind='repository_repair' AND project_id IS NOT NULL AND base_sha IS NOT NULL
  AND business_scope_id IS NULL)
 OR (kind='business' AND business_scope_id IS NOT NULL
  AND project_id IS NULL AND base_sha IS NULL)
);
ALTER TABLE app.tasks DROP CONSTRAINT tasks_phase_check;
ALTER TABLE app.tasks ADD CONSTRAINT tasks_phase_check CHECK (
 (kind='repository_repair' AND phase IN
  ('prepare','reproduce','repair','verify','deliver','finalize'))
 OR (kind='business' AND phase IN
  ('prepare','decide','plan','validate','human','execute','verify','finalize'))
);
ALTER TABLE app.events ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE app.events ADD CONSTRAINT events_task_fk
 FOREIGN KEY (task_id) REFERENCES app.tasks(id) DEFERRABLE INITIALLY DEFERRED;
CREATE FUNCTION app.check_event_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (
  SELECT 1 FROM app.events e JOIN app.tasks t ON t.id=e.task_id
  WHERE e.task_id=NEW.task_id AND e.seq=NEW.seq
   AND e.project_id IS DISTINCT FROM t.project_id
 ) THEN
  RAISE EXCEPTION 'Event project must match task project' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER events_scope_check AFTER INSERT OR UPDATE ON app.events
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_event_scope();
CREATE FUNCTION app.check_task_event_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (
  SELECT 1 FROM app.events e JOIN app.tasks t ON t.id=e.task_id
  WHERE t.id=NEW.id AND e.project_id IS DISTINCT FROM t.project_id
 ) THEN
  RAISE EXCEPTION 'Task project must match event projects' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER tasks_event_scope_check AFTER UPDATE OF project_id ON app.tasks
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_task_event_scope();
CREATE INDEX tasks_business_queue ON app.tasks(kind,state,created_at)
 WHERE kind='business';
