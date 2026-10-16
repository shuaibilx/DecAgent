"""Explicit registration and contract digest fencing, backed by PostgreSQL."""

from uuid import uuid4

from psycopg.types.json import Jsonb

from decagent.business.contracts import PluginSpec


class PluginRegistry:
    def __init__(self, database):
        self.database = database

    def register(self, manifest):
        spec = PluginSpec.from_mapping(manifest)
        with self.database.connect() as conn:
            return conn.execute(
                "INSERT INTO app.business_scopes(id,plugin_id,plugin_version,manifest_digest,"
                "manifest) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(plugin_id) DO UPDATE SET "
                "plugin_version=EXCLUDED.plugin_version,manifest_digest=EXCLUDED.manifest_digest,"
                "manifest=EXCLUDED.manifest,enabled=true,revision=app.business_scopes.revision+"
                "CASE WHEN app.business_scopes.manifest_digest<>EXCLUDED.manifest_digest OR "
                "NOT app.business_scopes.enabled THEN 1 ELSE 0 END RETURNING *",
                (uuid4(), spec.id, spec.version, spec.digest, Jsonb(spec.manifest)),
            ).fetchone()

    def list(self):
        with self.database.connect() as conn:
            return conn.execute("SELECT * FROM app.business_scopes ORDER BY plugin_id").fetchall()

    def _registered(self, scope_id):
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM app.business_scopes WHERE id=%s", (scope_id,)
            ).fetchone()
        if not row:
            raise KeyError("Business plugin scope not found")
        spec = PluginSpec.from_mapping(row["manifest"])
        if (
            spec.digest != row["manifest_digest"]
            or spec.version != row["plugin_version"]
            or spec.id != row["plugin_id"]
        ):
            raise ValueError("Registered plugin contract integrity changed")
        return row, spec

    def inspect(self, scope_id):
        row, spec = self._registered(scope_id)
        return {**row, "manifest": spec.manifest}

    def get(self, scope_id, expected_digest=None):
        row, spec = self._registered(scope_id)
        if not row["enabled"]:
            raise ValueError("Business plugin is disabled; explicit review required")
        if expected_digest is not None and spec.digest != expected_digest:
            raise ValueError("Plugin contract changed; explicit review required")
        return spec

    def disable(self, scope_id):
        with self.database.connect() as conn:
            row = conn.execute(
                "UPDATE app.business_scopes SET enabled=false,revision=revision+"
                "CASE WHEN enabled THEN 1 ELSE 0 END WHERE id=%s RETURNING *",
                (scope_id,),
            ).fetchone()
            if not row:
                raise KeyError("Business plugin scope not found")
            return row
