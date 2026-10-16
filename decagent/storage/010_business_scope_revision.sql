-- Historical unbound operation stamps stay unknown and cannot regain execution authority.
ALTER TABLE business.operations ADD COLUMN scope_revision integer CHECK (scope_revision>0);
ALTER TABLE business.execution_grants ADD COLUMN scope_revision integer CHECK (scope_revision>0);
UPDATE business.operations SET state='failed',error_code='contract_changed'
 WHERE state IN ('ready','awaiting_approval');
CREATE FUNCTION business.keep_operation_scope_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.scope_revision IS DISTINCT FROM OLD.scope_revision THEN
  RAISE EXCEPTION 'Prepared scope revision is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER operation_scope_immutable BEFORE UPDATE ON business.operations
 FOR EACH ROW EXECUTE FUNCTION business.keep_operation_scope_revision();
