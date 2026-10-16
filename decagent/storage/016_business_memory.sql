-- Employee business memory is independent of repository/PR memory activation.
CREATE TABLE app.business_memory_rules (
 rule_id text NOT NULL CHECK(length(rule_id) BETWEEN 1 AND 200),
 rule_version text NOT NULL CHECK(length(rule_version) BETWEEN 1 AND 200),
 company_scope text NOT NULL CHECK(company_scope='local-company'),
 scope_id uuid NOT NULL REFERENCES app.business_scopes(id),
 scope_revision integer NOT NULL CHECK(scope_revision>0),
 contract_fingerprint text NOT NULL CHECK(contract_fingerprint ~ '^[a-f0-9]{64}$'),
 question_fingerprint text NOT NULL CHECK(question_fingerprint ~ '^[a-f0-9]{64}$'),
 plan_fingerprint text NOT NULL CHECK(plan_fingerprint ~ '^[a-f0-9]{64}$'),
 command text NOT NULL CHECK(command IN
  ('create','assign','start','comment','resolve','close','reopen','set_priority')),
 renderer_version text NOT NULL CHECK(renderer_version='verified-command-1'),
 lesson text NOT NULL CHECK(length(trim(lesson)) BETWEEN 1 AND 512),
 validation_refs jsonb NOT NULL CHECK(jsonb_typeof(validation_refs)='array'
  AND jsonb_array_length(validation_refs) BETWEEN 1 AND 16
  AND octet_length(validation_refs::text)<=32768),
 content_digest text NOT NULL CHECK(content_digest ~ '^[a-f0-9]{64}$'),
 status text NOT NULL CHECK(status IN ('published','revoked')),
 published_by text NOT NULL CHECK(published_by='owner'),
 published_at timestamptz NOT NULL, valid_until timestamptz NOT NULL,
 PRIMARY KEY(rule_id,rule_version), CHECK(valid_until>published_at)
);

CREATE TABLE app.business_memories (
 id uuid PRIMARY KEY,
 company_scope text NOT NULL CHECK(company_scope='local-company'),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 kind text NOT NULL CHECK(kind IN ('confirmed_preference','verified_workflow_experience')),
 subject text NOT NULL CHECK(length(subject) BETWEEN 1 AND 200),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object' AND octet_length(content::text)<=8192),
 evidence_refs jsonb NOT NULL CHECK(jsonb_typeof(evidence_refs)='array'
  AND jsonb_array_length(evidence_refs) BETWEEN 1 AND 16
  AND octet_length(evidence_refs::text)<=32768),
 scope_id uuid NOT NULL REFERENCES app.business_scopes(id),
 scope_revision integer NOT NULL CHECK(scope_revision>0),
 account_revision bigint NOT NULL CHECK(account_revision>0),
 contract_fingerprint text NOT NULL CHECK(contract_fingerprint ~ '^[a-f0-9]{64}$'),
 question_fingerprint text NOT NULL CHECK(question_fingerprint ~ '^[a-f0-9]{64}$'),
 plan_fingerprint text NOT NULL CHECK(plan_fingerprint ~ '^[a-f0-9]{64}$'),
 status text NOT NULL CHECK(status IN ('pending_confirmation','active','revoked','superseded')),
 verified_at timestamptz, expires_at timestamptz NOT NULL,
 confirmed_by uuid REFERENCES business.employee_accounts(id),
 supersedes uuid REFERENCES app.business_memories(id),
 rule_id text, rule_version text,
 content_digest text NOT NULL CHECK(content_digest ~ '^[a-f0-9]{64}$'),
 created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
 FOREIGN KEY(rule_id,rule_version) REFERENCES app.business_memory_rules(rule_id,rule_version),
 UNIQUE(company_scope,employee_id,content_digest),
 CHECK(supersedes IS NULL OR supersedes<>id),
 CHECK(expires_at>created_at),
 CHECK(status<>'active' OR verified_at IS NOT NULL),
 CHECK((kind='confirmed_preference'
    AND subject IN ('response_language','response_style','ticket_category_preference','communication_preference')
    AND rule_id IS NULL AND rule_version IS NULL
    AND (verified_at IS NULL OR confirmed_by=employee_id))
  OR (kind='verified_workflow_experience' AND status<>'pending_confirmation'
    AND rule_id IS NOT NULL AND rule_version IS NOT NULL AND confirmed_by IS NULL))
);
CREATE INDEX business_memory_candidates ON app.business_memories
 (company_scope,employee_id,scope_id,status,expires_at);

CREATE TABLE app.business_memory_source_revocations (
 company_scope text NOT NULL CHECK(company_scope='local-company'),
 employee_id uuid NOT NULL REFERENCES business.employee_accounts(id),
 scope_id uuid NOT NULL REFERENCES app.business_scopes(id),
 evidence_key text NOT NULL CHECK(evidence_key ~ '^[a-f0-9]{64}$'),
 reference jsonb NOT NULL CHECK(jsonb_typeof(reference)='object'),
 reason text NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 512),
 revoked_at timestamptz NOT NULL,
 PRIMARY KEY(company_scope,employee_id,evidence_key)
);

CREATE TABLE app.business_memory_audit (
 id uuid PRIMARY KEY,
 memory_id uuid REFERENCES app.business_memories(id),
 actor_employee_id uuid REFERENCES business.employee_accounts(id),
 event text NOT NULL CHECK(event IN
  ('preference_proposed','preference_confirmed','memory_revoked','memory_superseded',
   'experience_recorded','source_revoked','rule_published','rule_revoked')),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=16384),
 created_at timestamptz NOT NULL
);

CREATE FUNCTION app.guard_business_memory() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'Business memory evidence is retained' USING ERRCODE='23514';
 END IF;
 IF (to_jsonb(NEW)-ARRAY['status','verified_at','expires_at','confirmed_by','updated_at'])
    IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['status','verified_at','expires_at','confirmed_by','updated_at']) THEN
  RAISE EXCEPTION 'Business memory content is immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.status='pending_confirmation' AND NEW.status='active' THEN
  IF NEW.kind<>'confirmed_preference' OR NEW.verified_at IS NULL
     OR NEW.confirmed_by IS DISTINCT FROM NEW.employee_id
     OR NEW.expires_at<>NEW.verified_at+interval '30 days' THEN
   RAISE EXCEPTION 'Preference requires bounded original employee confirmation' USING ERRCODE='23514';
  END IF;
 ELSIF OLD.status IN ('pending_confirmation','active')
       AND NEW.status IN ('revoked','superseded') THEN
  IF (NEW.verified_at,NEW.expires_at,NEW.confirmed_by)
      IS DISTINCT FROM (OLD.verified_at,OLD.expires_at,OLD.confirmed_by) THEN
   RAISE EXCEPTION 'Memory validity history is retained' USING ERRCODE='23514';
  END IF;
 ELSIF NEW IS DISTINCT FROM OLD THEN
  RAISE EXCEPTION 'Memory cannot be renewed or reactivated in place' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER business_memory_guard BEFORE UPDATE OR DELETE ON app.business_memories
 FOR EACH ROW EXECUTE FUNCTION app.guard_business_memory();

CREATE FUNCTION app.guard_business_memory_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status')
    OR (OLD.status='revoked' AND NEW.status<>'revoked') THEN
  RAISE EXCEPTION 'Published memory rules and revocations are immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER business_memory_rule_guard BEFORE UPDATE OR DELETE ON app.business_memory_rules
 FOR EACH ROW EXECUTE FUNCTION app.guard_business_memory_rule();

CREATE FUNCTION app.keep_business_memory_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Business memory audit and source revocations are append only' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER business_memory_audit_guard BEFORE UPDATE OR DELETE ON app.business_memory_audit
 FOR EACH ROW EXECUTE FUNCTION app.keep_business_memory_audit();
CREATE TRIGGER business_memory_source_guard BEFORE UPDATE OR DELETE
 ON app.business_memory_source_revocations
 FOR EACH ROW EXECUTE FUNCTION app.keep_business_memory_audit();
