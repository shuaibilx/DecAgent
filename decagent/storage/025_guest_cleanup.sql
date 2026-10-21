ALTER TABLE business.workspaces
 ADD COLUMN cleanup_completed_at timestamptz;

-- Keep only irreversible-clear proof so a lost response can be retried after
-- guest data and employee credentials have been physically removed.
CREATE TABLE business.guest_workspace_tombstones (
 token_hash text PRIMARY KEY CHECK (token_hash ~ '^[a-f0-9]{64}$'),
 workspace_id uuid NOT NULL REFERENCES business.workspaces(id),
 csrf_hash text NOT NULL CHECK (csrf_hash ~ '^[a-f0-9]{64}$'),
 cleared_at timestamptz NOT NULL DEFAULT now()
);

-- Tenant-owned records contain intentional cycles (tasks/actions/operations,
-- plans, and memories). Defer only relations where both endpoints are tenant
-- scoped; constraints remain enforced when the transaction commits.
DO $$
DECLARE relation record;
BEGIN
 FOR relation IN
  SELECT child_ns.nspname AS child_schema,child.relname AS child_table,fk.conname
  FROM pg_constraint fk
  JOIN pg_class child ON child.oid=fk.conrelid
  JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
  JOIN pg_class parent ON parent.oid=fk.confrelid
  JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
  WHERE fk.contype='f' AND NOT fk.condeferrable
   AND child_ns.nspname IN ('app','business')
   AND parent_ns.nspname IN ('app','business')
   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=child.oid
               AND a.attname='workspace_id' AND NOT a.attisdropped)
   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=parent.oid
               AND a.attname='workspace_id' AND NOT a.attisdropped)
 LOOP
  EXECUTE format('ALTER TABLE %I.%I ALTER CONSTRAINT %I DEFERRABLE INITIALLY DEFERRED',
                 relation.child_schema,relation.child_table,relation.conname);
 END LOOP;
END $$;
