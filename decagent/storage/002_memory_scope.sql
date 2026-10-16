-- Preserve all records; fail migration if existing replacement links cross projects.
ALTER TABLE app.memory_versions ADD CONSTRAINT memory_versions_id_project_unique
 UNIQUE(id,project_id);
ALTER TABLE app.memory_versions DROP CONSTRAINT memory_versions_replaces_fkey;
ALTER TABLE app.memory_versions ADD CONSTRAINT memory_versions_replaces_project_fkey
 FOREIGN KEY(replaces,project_id) REFERENCES app.memory_versions(id,project_id)
 DEFERRABLE INITIALLY DEFERRED;
