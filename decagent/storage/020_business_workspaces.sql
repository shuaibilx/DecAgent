-- Workspace tenancy is added without rewriting legacy identifiers or task checkpoints.
SET CONSTRAINTS ALL IMMEDIATE;
CREATE TABLE business.workspaces (
 id uuid PRIMARY KEY,
 slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{0,62}$'),
 kind text NOT NULL CHECK (kind IN ('legacy','guest')),
 active boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (id,slug)
);

INSERT INTO business.workspaces(id,slug,kind)
VALUES ('00000000-0000-0000-0000-000000000001','legacy','legacy');

-- Existing callers remain bound to the sole pre-existing workspace. New tenant-aware
-- writers must provide an explicit workspace and child ownership in their SQL.
ALTER TABLE business.people
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.categories
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.tickets
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.ticket_history
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.operation_receipts
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.employee_accounts
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.employee_sessions
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.mcp_credentials
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.employee_audit
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.operations
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.operation_approvals
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE business.execution_grants
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';

ALTER TABLE app.tasks
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.events
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.actions
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.action_attempts
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.approvals
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.verification_runs
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.decision_records
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.usage_records
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.deliveries
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';

ALTER TABLE app.decision_validation_reports
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.decision_policies
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.task_decision_thresholds
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.task_decision_policy_bindings
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.decision_policy_events
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_plan_revisions
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_plan_heads
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_plan_nodes
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_decision_interventions
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_optional_intervention_rules
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';

ALTER TABLE app.business_memory_rules
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_memories
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_memory_source_revocations
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_memory_audit
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.workflow_validation_reports
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.business_workflows
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.workflow_instances
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE app.workflow_events
 ADD COLUMN workspace_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';

-- Composite keys let foreign keys enforce ownership, not merely object existence.
ALTER TABLE business.people ADD CONSTRAINT people_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE business.categories ADD CONSTRAINT categories_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE business.categories DROP CONSTRAINT categories_name_key;
ALTER TABLE business.categories ADD CONSTRAINT categories_workspace_name_unique UNIQUE(workspace_id,name);
ALTER TABLE business.tickets ADD CONSTRAINT tickets_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE business.employee_accounts ADD CONSTRAINT employee_accounts_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE business.employee_accounts ADD CONSTRAINT employee_accounts_person_workspace_unique UNIQUE(person_id,workspace_id);
ALTER TABLE business.employee_sessions ADD CONSTRAINT employee_sessions_hash_workspace_unique UNIQUE(token_hash,workspace_id);
ALTER TABLE business.mcp_credentials ADD CONSTRAINT mcp_credentials_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE business.mcp_credentials ADD CONSTRAINT mcp_credentials_actor_workspace_unique UNIQUE(id,employee_id,workspace_id);
ALTER TABLE business.operations ADD CONSTRAINT operations_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE business.operations ADD CONSTRAINT operations_actor_workspace_unique UNIQUE(id,employee_id,workspace_id);
ALTER TABLE business.operation_receipts ADD CONSTRAINT operation_receipts_id_workspace_unique UNIQUE(operation_id,workspace_id);

ALTER TABLE app.tasks ADD CONSTRAINT tasks_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE app.actions ADD CONSTRAINT actions_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE app.actions ADD CONSTRAINT actions_id_task_workspace_unique UNIQUE(id,task_id,workspace_id);
ALTER TABLE app.approvals ADD CONSTRAINT approvals_id_task_workspace_unique UNIQUE(id,task_id,workspace_id);
ALTER TABLE app.decision_validation_reports ADD CONSTRAINT decision_reports_id_workspace_unique
 UNIQUE(report_id,workspace_id);
ALTER TABLE app.decision_policies ADD CONSTRAINT decision_policies_version_workspace_unique
 UNIQUE(policy_id,policy_version,workspace_id);
ALTER TABLE app.business_memory_rules ADD CONSTRAINT business_memory_rules_version_workspace_unique
 UNIQUE(rule_id,rule_version,workspace_id);
ALTER TABLE app.business_plan_revisions ADD CONSTRAINT business_plan_revisions_task_workspace_unique
 UNIQUE(task_id,revision,workspace_id);
ALTER TABLE app.business_plan_revisions ADD CONSTRAINT business_plan_revisions_digest_workspace_unique
 UNIQUE(task_id,revision,content_digest,workspace_id);
ALTER TABLE app.business_plan_nodes ADD CONSTRAINT business_plan_nodes_task_workspace_unique
 UNIQUE(task_id,node_id,execution_generation,workspace_id);
ALTER TABLE app.business_memories ADD CONSTRAINT business_memories_id_workspace_unique UNIQUE(id,workspace_id);
ALTER TABLE app.workflow_validation_reports ADD CONSTRAINT workflow_reports_id_workspace_unique UNIQUE(report_id,workspace_id);
ALTER TABLE app.business_workflows ADD CONSTRAINT business_workflows_workspace_version_unique
 UNIQUE(workflow_id,version,workspace_id);
ALTER TABLE app.business_workflows ADD CONSTRAINT business_workflows_workspace_digest_unique
 UNIQUE(workflow_id,version,definition_digest,workspace_id);

ALTER TABLE business.people ADD CONSTRAINT people_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.categories ADD CONSTRAINT categories_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.tickets ADD CONSTRAINT tickets_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.ticket_history ADD CONSTRAINT ticket_history_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operation_receipts ADD CONSTRAINT operation_receipts_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_accounts ADD CONSTRAINT employee_accounts_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_sessions ADD CONSTRAINT employee_sessions_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.mcp_credentials ADD CONSTRAINT mcp_credentials_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_audit ADD CONSTRAINT employee_audit_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operations ADD CONSTRAINT operations_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operation_approvals ADD CONSTRAINT operation_approvals_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.execution_grants ADD CONSTRAINT execution_grants_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE app.tasks ADD CONSTRAINT tasks_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.events ADD CONSTRAINT events_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.actions ADD CONSTRAINT actions_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.action_attempts ADD CONSTRAINT action_attempts_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.approvals ADD CONSTRAINT approvals_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.verification_runs ADD CONSTRAINT verification_runs_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_records ADD CONSTRAINT decision_records_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.usage_records ADD CONSTRAINT usage_records_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.deliveries ADD CONSTRAINT deliveries_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_validation_reports ADD CONSTRAINT decision_reports_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_policies ADD CONSTRAINT decision_policies_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.task_decision_thresholds ADD CONSTRAINT task_decision_thresholds_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.task_decision_policy_bindings ADD CONSTRAINT task_decision_bindings_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_policy_events ADD CONSTRAINT decision_policy_events_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_revisions ADD CONSTRAINT business_plan_revisions_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_heads ADD CONSTRAINT business_plan_heads_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_nodes ADD CONSTRAINT business_plan_nodes_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_decision_interventions ADD CONSTRAINT decision_interventions_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_optional_intervention_rules ADD CONSTRAINT optional_intervention_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_rules ADD CONSTRAINT business_memory_rules_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memories ADD CONSTRAINT business_memories_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_source_revocations ADD CONSTRAINT memory_revocations_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_audit ADD CONSTRAINT business_memory_audit_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_validation_reports ADD CONSTRAINT workflow_reports_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_workflows ADD CONSTRAINT business_workflows_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_instances ADD CONSTRAINT workflow_instances_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_events ADD CONSTRAINT workflow_events_workspace_fk
 FOREIGN KEY(workspace_id) REFERENCES business.workspaces(id) DEFERRABLE INITIALLY DEFERRED;

-- Same-workspace relation constraints for business principals and resources.
ALTER TABLE business.employee_accounts ADD CONSTRAINT employee_person_same_workspace_fk
 FOREIGN KEY(person_id,workspace_id) REFERENCES business.people(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_sessions ADD CONSTRAINT employee_session_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.mcp_credentials ADD CONSTRAINT mcp_credential_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.tickets ADD CONSTRAINT ticket_category_same_workspace_fk
 FOREIGN KEY(category_id,workspace_id) REFERENCES business.categories(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.tickets ADD CONSTRAINT ticket_requester_same_workspace_fk
 FOREIGN KEY(requester_id,workspace_id) REFERENCES business.people(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.tickets ADD CONSTRAINT ticket_assignee_same_workspace_fk
 FOREIGN KEY(assignee_id,workspace_id) REFERENCES business.people(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.tickets ADD CONSTRAINT ticket_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.ticket_history ADD CONSTRAINT ticket_history_ticket_same_workspace_fk
 FOREIGN KEY(ticket_id,workspace_id) REFERENCES business.tickets(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.ticket_history ADD CONSTRAINT ticket_history_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operation_receipts ADD CONSTRAINT receipt_ticket_same_workspace_fk
 FOREIGN KEY(resource_id,workspace_id) REFERENCES business.tickets(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operation_receipts ADD CONSTRAINT receipt_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_audit ADD CONSTRAINT employee_audit_actor_same_workspace_fk
 FOREIGN KEY(actor_employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_audit ADD CONSTRAINT employee_audit_ticket_same_workspace_fk
 FOREIGN KEY(ticket_id,workspace_id) REFERENCES business.tickets(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.employee_audit ADD CONSTRAINT employee_audit_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE app.tasks ADD CONSTRAINT task_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.tasks ADD CONSTRAINT task_credential_same_workspace_fk
 FOREIGN KEY(credential_ref,employee_id,workspace_id)
 REFERENCES business.mcp_credentials(id,employee_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.events ADD CONSTRAINT event_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.actions ADD CONSTRAINT action_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.action_attempts ADD CONSTRAINT action_attempt_same_workspace_fk
 FOREIGN KEY(action_id,workspace_id) REFERENCES app.actions(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.approvals ADD CONSTRAINT approval_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.approvals ADD CONSTRAINT approval_action_same_workspace_fk
 FOREIGN KEY(action_id,task_id,workspace_id) REFERENCES app.actions(id,task_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.verification_runs ADD CONSTRAINT verification_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_records ADD CONSTRAINT decision_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.usage_records ADD CONSTRAINT usage_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.deliveries ADD CONSTRAINT delivery_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE business.operations ADD CONSTRAINT operation_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operations ADD CONSTRAINT operation_task_same_workspace_fk
 FOREIGN KEY(agent_task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operations ADD CONSTRAINT operation_action_same_workspace_fk
 FOREIGN KEY(agent_action_id,agent_task_id,workspace_id)
 REFERENCES app.actions(id,task_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operations ADD CONSTRAINT operation_supersedes_same_workspace_fk
 FOREIGN KEY(supersedes,workspace_id) REFERENCES business.operations(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.operation_approvals ADD CONSTRAINT operation_approval_same_workspace_fk
 FOREIGN KEY(operation_id,employee_id,workspace_id)
 REFERENCES business.operations(id,employee_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.execution_grants ADD CONSTRAINT grant_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.execution_grants ADD CONSTRAINT grant_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE business.execution_grants ADD CONSTRAINT grant_action_same_workspace_fk
 FOREIGN KEY(action_id,task_id,workspace_id) REFERENCES app.actions(id,task_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE app.decision_policies ADD CONSTRAINT decision_policy_report_same_workspace_fk
 FOREIGN KEY(validation_record,workspace_id)
 REFERENCES app.decision_validation_reports(report_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.task_decision_thresholds ADD CONSTRAINT threshold_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.task_decision_thresholds ADD CONSTRAINT threshold_policy_same_workspace_fk
 FOREIGN KEY(policy_id,policy_version,workspace_id)
 REFERENCES app.decision_policies(policy_id,policy_version,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.task_decision_policy_bindings ADD CONSTRAINT decision_binding_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.task_decision_policy_bindings ADD CONSTRAINT decision_binding_policy_same_workspace_fk
 FOREIGN KEY(policy_id,policy_version,workspace_id)
 REFERENCES app.decision_policies(policy_id,policy_version,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_policy_events ADD CONSTRAINT decision_event_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.decision_policy_events ADD CONSTRAINT decision_event_policy_same_workspace_fk
 FOREIGN KEY(policy_id,policy_version,workspace_id)
 REFERENCES app.decision_policies(policy_id,policy_version,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_revisions ADD CONSTRAINT plan_revision_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_revisions ADD CONSTRAINT plan_parent_same_workspace_fk
 FOREIGN KEY(task_id,parent_revision,parent_digest,workspace_id)
 REFERENCES app.business_plan_revisions(task_id,revision,content_digest,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_heads ADD CONSTRAINT plan_head_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_heads ADD CONSTRAINT plan_head_revision_same_workspace_fk
 FOREIGN KEY(task_id,current_revision,workspace_id)
 REFERENCES app.business_plan_revisions(task_id,revision,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_nodes ADD CONSTRAINT plan_node_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_nodes ADD CONSTRAINT plan_node_revision_same_workspace_fk
 FOREIGN KEY(task_id,plan_revision,workspace_id)
 REFERENCES app.business_plan_revisions(task_id,revision,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_plan_nodes ADD CONSTRAINT plan_node_action_same_workspace_fk
 FOREIGN KEY(action_id,task_id,workspace_id) REFERENCES app.actions(id,task_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_decision_interventions ADD CONSTRAINT intervention_approval_same_workspace_fk
 FOREIGN KEY(approval_id,task_id,workspace_id) REFERENCES app.approvals(id,task_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_decision_interventions ADD CONSTRAINT intervention_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_decision_interventions ADD CONSTRAINT intervention_employee_same_workspace_fk
 FOREIGN KEY(actor_employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_decision_interventions ADD CONSTRAINT intervention_node_same_workspace_fk
 FOREIGN KEY(task_id,node_id,execution_generation,workspace_id)
 REFERENCES app.business_plan_nodes(task_id,node_id,execution_generation,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_decision_interventions ADD CONSTRAINT intervention_plan_same_workspace_fk
 FOREIGN KEY(task_id,plan_revision,plan_digest,workspace_id)
 REFERENCES app.business_plan_revisions(task_id,revision,content_digest,workspace_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE app.business_memories ADD CONSTRAINT memory_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memories ADD CONSTRAINT memory_confirmer_same_workspace_fk
 FOREIGN KEY(confirmed_by,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memories ADD CONSTRAINT memory_supersedes_same_workspace_fk
 FOREIGN KEY(supersedes,workspace_id) REFERENCES app.business_memories(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memories ADD CONSTRAINT memory_rule_same_workspace_fk
 FOREIGN KEY(rule_id,rule_version,workspace_id)
 REFERENCES app.business_memory_rules(rule_id,rule_version,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_source_revocations ADD CONSTRAINT memory_revocation_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_audit ADD CONSTRAINT memory_audit_memory_same_workspace_fk
 FOREIGN KEY(memory_id,workspace_id) REFERENCES app.business_memories(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_audit ADD CONSTRAINT memory_audit_employee_same_workspace_fk
 FOREIGN KEY(actor_employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE app.business_workflows ADD CONSTRAINT workflow_report_same_workspace_fk
 FOREIGN KEY(validation_report_id,workspace_id)
 REFERENCES app.workflow_validation_reports(report_id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_instances ADD CONSTRAINT workflow_instance_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_instances ADD CONSTRAINT workflow_instance_employee_same_workspace_fk
 FOREIGN KEY(employee_id,workspace_id) REFERENCES business.employee_accounts(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_instances ADD CONSTRAINT workflow_instance_definition_same_workspace_fk
 FOREIGN KEY(workflow_id,version,definition_digest,workspace_id)
 REFERENCES app.business_workflows(workflow_id,version,definition_digest,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_events ADD CONSTRAINT workflow_event_definition_same_workspace_fk
 FOREIGN KEY(workflow_id,version,workspace_id)
 REFERENCES app.business_workflows(workflow_id,version,workspace_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.workflow_events ADD CONSTRAINT workflow_event_task_same_workspace_fk
 FOREIGN KEY(task_id,workspace_id) REFERENCES app.tasks(id,workspace_id) DEFERRABLE INITIALLY DEFERRED;
