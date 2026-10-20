-- Replace the old fixed company label with the authenticated workspace identity.
SET CONSTRAINTS ALL DEFERRED;

DROP TRIGGER business_memory_guard ON app.business_memories;
DROP TRIGGER business_memory_rule_guard ON app.business_memory_rules;
DROP TRIGGER business_memory_audit_guard ON app.business_memory_audit;
DROP TRIGGER business_memory_source_guard ON app.business_memory_source_revocations;

-- Keep every pre-workspace memory with its original employee and migrate newer
-- records from that employee's authoritative workspace row.
UPDATE app.business_memories m
SET workspace_id=a.workspace_id
FROM business.employee_accounts a
WHERE a.id=m.employee_id AND m.workspace_id IS DISTINCT FROM a.workspace_id;

UPDATE app.business_memory_source_revocations r
SET workspace_id=a.workspace_id
FROM business.employee_accounts a
WHERE a.id=r.employee_id AND r.workspace_id IS DISTINCT FROM a.workspace_id;

-- Published rules are tenant-local. All source receipts for one rule must share
-- one workspace; abort instead of guessing if historical evidence disagrees.
DO $$
DECLARE
 rule_row record;
 source_count integer;
 source_workspace uuid;
BEGIN
 FOR rule_row IN
  SELECT rule_id,rule_version,validation_refs FROM app.business_memory_rules
 LOOP
  SELECT count(DISTINCT o.workspace_id),min(o.workspace_id::text)::uuid
  INTO source_count,source_workspace
  FROM jsonb_array_elements(rule_row.validation_refs) AS item(value)
  JOIN business.operations o ON o.id=item.value->>'operation_id'
  WHERE item.value->>'kind'='operation_receipt';
  IF source_count>1 THEN
   RAISE EXCEPTION 'Business memory rule %/% has sources from multiple workspaces',
    rule_row.rule_id,rule_row.rule_version;
  END IF;
  IF source_count=1 THEN
   UPDATE app.business_memory_rules
   SET workspace_id=source_workspace
   WHERE rule_id=rule_row.rule_id AND rule_version=rule_row.rule_version;
  END IF;
 END LOOP;
END;
$$;

UPDATE app.business_memory_audit a
SET workspace_id=COALESCE(
 (SELECT m.workspace_id FROM app.business_memories m WHERE m.id=a.memory_id),
 (SELECT e.workspace_id FROM business.employee_accounts e WHERE e.id=a.actor_employee_id),
 (SELECT r.workspace_id FROM app.business_memory_rules r
  WHERE r.rule_id=a.payload->>'rule_id' AND r.rule_version=a.payload->>'rule_version'),
 a.workspace_id
);

-- Remove both the legacy and workspace-aware rule foreign keys before replacing
-- the global rule key with a tenant-local key.
DO $$
DECLARE
 fk record;
BEGIN
 FOR fk IN
  SELECT conname FROM pg_constraint
  WHERE conrelid='app.business_memories'::regclass
    AND confrelid='app.business_memory_rules'::regclass
 LOOP
  EXECUTE format('ALTER TABLE app.business_memories DROP CONSTRAINT %I',fk.conname);
 END LOOP;
END;
$$;

ALTER TABLE app.business_memory_rules
 DROP CONSTRAINT business_memory_rules_version_workspace_unique,
 DROP CONSTRAINT business_memory_rules_pkey,
 ADD CONSTRAINT business_memory_rules_pkey PRIMARY KEY(rule_id,rule_version,workspace_id);

DROP INDEX IF EXISTS app.business_memory_candidates;
ALTER TABLE app.business_memory_rules DROP COLUMN company_scope CASCADE;
ALTER TABLE app.business_memories DROP COLUMN company_scope CASCADE;
ALTER TABLE app.business_memory_source_revocations DROP COLUMN company_scope CASCADE;

ALTER TABLE app.business_memories
 ADD CONSTRAINT business_memories_workspace_employee_digest_unique
 UNIQUE(workspace_id,employee_id,content_digest),
 ADD CONSTRAINT memory_rule_same_workspace_fk
 FOREIGN KEY(rule_id,rule_version,workspace_id)
 REFERENCES app.business_memory_rules(rule_id,rule_version,workspace_id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.business_memory_source_revocations
 ADD CONSTRAINT business_memory_source_revocations_pkey
 PRIMARY KEY(workspace_id,employee_id,evidence_key);

CREATE INDEX business_memory_candidates
 ON app.business_memories(workspace_id,employee_id,scope_id,status,expires_at);

CREATE TRIGGER business_memory_guard BEFORE UPDATE OR DELETE ON app.business_memories
 FOR EACH ROW EXECUTE FUNCTION app.guard_business_memory();
CREATE TRIGGER business_memory_rule_guard BEFORE UPDATE OR DELETE ON app.business_memory_rules
 FOR EACH ROW EXECUTE FUNCTION app.guard_business_memory_rule();
CREATE TRIGGER business_memory_audit_guard BEFORE UPDATE OR DELETE ON app.business_memory_audit
 FOR EACH ROW EXECUTE FUNCTION app.keep_business_memory_audit();
CREATE TRIGGER business_memory_source_guard BEFORE UPDATE OR DELETE
 ON app.business_memory_source_revocations
 FOR EACH ROW EXECUTE FUNCTION app.keep_business_memory_audit();
