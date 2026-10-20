-- Task-owned records inherit their tenant from the trusted task row. This keeps
-- older internal writers safe while the application migrates to explicit scope.
CREATE FUNCTION app.inherit_task_workspace() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
 trusted_workspace uuid;
 parent_task uuid;
BEGIN
 IF TG_ARGV[0] = 'action' THEN
   SELECT workspace_id INTO trusted_workspace
   FROM app.actions WHERE id=NEW.action_id;
 ELSE
   parent_task := NEW.task_id;
   IF parent_task IS NULL THEN
     RETURN NEW;
   END IF;
   SELECT workspace_id INTO trusted_workspace
   FROM app.tasks WHERE id=parent_task;
 END IF;
 IF trusted_workspace IS NULL THEN
   RAISE EXCEPTION 'Parent workspace unavailable for task-owned row'
     USING ERRCODE='23503';
 END IF;
 NEW.workspace_id := trusted_workspace;
 RETURN NEW;
END;
$$;

CREATE TRIGGER events_inherit_task_workspace BEFORE INSERT ON app.events
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER actions_inherit_task_workspace BEFORE INSERT ON app.actions
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER action_attempts_inherit_task_workspace BEFORE INSERT ON app.action_attempts
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('action');
CREATE TRIGGER approvals_inherit_task_workspace BEFORE INSERT ON app.approvals
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER verification_runs_inherit_task_workspace BEFORE INSERT ON app.verification_runs
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER decision_records_inherit_task_workspace BEFORE INSERT ON app.decision_records
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER usage_records_inherit_task_workspace BEFORE INSERT ON app.usage_records
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER deliveries_inherit_task_workspace BEFORE INSERT ON app.deliveries
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER task_decision_thresholds_inherit_workspace
 BEFORE INSERT ON app.task_decision_thresholds
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER task_decision_bindings_inherit_workspace
 BEFORE INSERT ON app.task_decision_policy_bindings
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER decision_policy_events_inherit_task_workspace
 BEFORE INSERT ON app.decision_policy_events
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER business_plan_revisions_inherit_workspace
 BEFORE INSERT ON app.business_plan_revisions
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER business_plan_heads_inherit_workspace
 BEFORE INSERT ON app.business_plan_heads
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER business_plan_nodes_inherit_workspace
 BEFORE INSERT ON app.business_plan_nodes
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER business_interventions_inherit_workspace
 BEFORE INSERT ON app.business_decision_interventions
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER workflow_instances_inherit_workspace
 BEFORE INSERT ON app.workflow_instances
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
CREATE TRIGGER workflow_events_inherit_workspace
 BEFORE INSERT ON app.workflow_events
 FOR EACH ROW EXECUTE FUNCTION app.inherit_task_workspace('task');
