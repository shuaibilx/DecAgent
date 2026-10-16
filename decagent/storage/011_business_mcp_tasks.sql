-- Preserve v1 CLI checkpoints and receipts while excluding them from MCP dispatch.
ALTER TABLE app.tasks ADD COLUMN business_migration_pending boolean NOT NULL DEFAULT false;
ALTER TABLE app.tasks ADD CONSTRAINT business_migration_kind_check
 CHECK (NOT business_migration_pending OR kind='business');
UPDATE app.tasks SET business_migration_pending=true,state='recovering',
 lease_owner=NULL,lease_until=NULL,event_seq=event_seq+1
 WHERE kind='business' AND graph_version<>'business-2'
 AND state NOT IN ('succeeded','failed','cancelled');
INSERT INTO app.events(task_id,project_id,seq,kind,payload)
 SELECT id,project_id,event_seq,'business.migration_required',
 jsonb_build_object('kind','version','reason','Historical CLI task requires explicit review',
                    'graph_version',graph_version,'target_graph_version','business-2')
 FROM app.tasks WHERE business_migration_pending;

CREATE FUNCTION app.keep_business_employee() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.kind='business' AND OLD.employee_id IS NOT NULL
    AND NEW.employee_id IS DISTINCT FROM OLD.employee_id THEN
  RAISE EXCEPTION 'Business task employee is immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.business_migration_pending AND
    (NOT NEW.business_migration_pending OR NEW.graph_version IS DISTINCT FROM OLD.graph_version
     OR NEW.employee_id IS DISTINCT FROM OLD.employee_id) THEN
  RAISE EXCEPTION 'Historical CLI task requires explicit migration' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER business_employee_immutable BEFORE UPDATE ON app.tasks
 FOR EACH ROW EXECUTE FUNCTION app.keep_business_employee();
