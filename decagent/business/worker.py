"""Lease-fenced business queue consumer using the shared persistent checkpointer."""

from langgraph.types import Command
from psycopg.errors import UndefinedTable

from decagent.runtime.worker import WAIT_STATES, CheckpointIncompatible, Worker, _LeasePulse
from decagent.storage.database import SCHEMA_VERSION

from . import conditional_graph
from .graph import GRAPH_VERSION, MAX_STEPS, PHASES, build_graph

SUPPORTED_VERSIONS = frozenset({GRAPH_VERSION, conditional_graph.GRAPH_VERSION})


class BusinessWorker(Worker):
    kind = "business"

    def __init__(
        self,
        database,
        settings,
        runtime_config=None,
        *,
        graph_factory=None,
        conditional_graph_factory=None,
        conditional_services_factory=None,
        **kwargs,
    ):
        mcp_manager = kwargs.pop("mcp_manager", None)
        employee_auth = kwargs.pop("employee_auth", None)
        super().__init__(
            database,
            settings,
            runtime_config,
            graph_factory=graph_factory or build_graph,
            **kwargs,
        )
        self.employee_auth = employee_auth
        self.mcp_manager = mcp_manager
        self.conditional_graph_factory = conditional_graph_factory or conditional_graph.build_graph
        self.conditional_services_factory = conditional_services_factory
        self._owns_mcp_manager = False
        if self.services_factory is None:
            self._ensure_mcp()

    def _ensure_mcp(self):
        if self.employee_auth is None or self.mcp_manager is None:
            from decagent.business.employee_auth import EmployeeAuth
            from decagent.business.mcp_client import MCPConnectionManager
            from decagent.gateway.auth import OwnerAuth

            if self.employee_auth is None:
                origin = OwnerAuth(self.database, self.settings.public_url)
                self.employee_auth = EmployeeAuth(
                    self.database,
                    origin.origin,
                    origin.secure,
                    self.settings.data_dir / "demo-auth",
                )
            if self.mcp_manager is None:
                self.mcp_manager = MCPConnectionManager(
                    self.runtime_config.tickets_mcp_url, self.employee_auth
                )
                self._owns_mcp_manager = True

    def close(self):
        if self._owns_mcp_manager:
            self.mcp_manager.close()

    def _client(self, task):
        from decagent.business.mcp_client import MCPAuthenticationError

        self._ensure_mcp()
        try:
            token = self.employee_auth.resolve_credential_ref(task["id"])
            identity = self.employee_auth.authenticate_mcp_token(token)
            if (
                identity is None
                or identity.employee_id != task["employee_id"]
                or identity.workspace_id != task["workspace_id"]
            ):
                raise ValueError("Employee identity unavailable")
            return self.mcp_manager.client(identity, token)
        except ValueError as exc:
            raise MCPAuthenticationError("Original employee must reauthenticate this task") from exc

    def claim(self):
        # Schema compatibility applies to the process; graph compatibility applies
        # to each claimed task. A mixed queue must not reject all supported work.
        with self.database.connect() as conn:
            try:
                versions = conn.execute(
                    "SELECT version FROM app.schema_migrations ORDER BY version"
                ).fetchall()
            except UndefinedTable as exc:
                raise ValueError("Database is not initialized; run decagent migrate") from exc
            if [row["version"] for row in versions] != list(range(1, SCHEMA_VERSION + 1)):
                raise ValueError("Incompatible database schema; migration/upgrade required")
            return conn.execute(
                "WITH candidate AS (SELECT id FROM app.tasks WHERE kind='business' "
                "AND employee_id IS NOT NULL AND credential_ref IS NOT NULL "
                "AND NOT business_migration_pending "
                "AND (state='queued' OR (state='running' AND "
                "(lease_until IS NULL OR lease_until<=clock_timestamp()))) "
                "ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE app.tasks t SET state='running',lease_owner=%s,"
                "lease_generation=lease_generation+1,"
                "lease_until=clock_timestamp()+%s*interval '1 second' "
                "FROM candidate c WHERE t.id=c.id RETURNING t.*",
                (self.owner, self.lease_seconds),
            ).fetchone()

    def _input(self, graph, config, task):
        snapshot = graph.get_state(config)
        if snapshot.values:
            saved = snapshot.values.get("data")
            if not isinstance(saved, dict) or saved.get("task_id") != str(task["id"]):
                raise ValueError("Checkpoint belongs to another business task")
            if saved.get("graph_version") != task["graph_version"]:
                raise CheckpointIncompatible(
                    "Checkpoint business graph version is incompatible; upgrade required"
                )
            pending = self._interrupts(snapshot)
            if pending:
                resumes = {}
                for item in pending:
                    payload = item.value if isinstance(item.value, dict) else {}
                    approval_id = payload.get("approval_id")
                    if approval_id:
                        with self.database.connect() as conn:
                            reply = conn.execute(
                                "SELECT id,status,digest,kind,reply_payload FROM app.approvals "
                                "WHERE id=%s AND task_id=%s AND workspace_id=%s",
                                (approval_id, task["id"], task["workspace_id"]),
                            ).fetchone()
                        if (
                            not reply
                            or reply["status"] not in {"replied", "consumed"}
                            or not (reply["reply_payload"] or {}).get(
                                "semantic_record"
                                if task["graph_version"] == conditional_graph.GRAPH_VERSION
                                and payload.get("kind") == "decision"
                                else "policy_record"
                            )
                            or (payload.get("digest") and payload["digest"] != reply["digest"])
                            or payload.get("kind") != reply["kind"]
                        ):
                            return None
                        resumes[item.id] = {"approval_id": str(reply["id"])}
                    elif payload.get("kind") in {"budget", "reconciliation"}:
                        with self.database.connect() as conn:
                            request = conn.execute(
                                "SELECT payload FROM app.events WHERE task_id=%s "
                                "AND workspace_id=%s AND kind='task.resume_requested' "
                                "AND (%s::timestamptz IS NULL OR created_at>=%s::timestamptz) "
                                "ORDER BY seq DESC LIMIT 1",
                                (
                                    task["id"],
                                    task["workspace_id"],
                                    snapshot.created_at,
                                    snapshot.created_at,
                                ),
                            ).fetchone()
                        if not request:
                            return None
                        # The authenticated gateway changes the durable task budget separately.
                        resumes[item.id] = {**request["payload"], "kind": payload["kind"]}
                    else:
                        return None
                return Command(resume=resumes)
            # A crash can leave ordinary pending nodes. Continue those exact nodes with None.
            return None
        if task["graph_version"] == conditional_graph.GRAPH_VERSION:
            return {
                "data": {
                    "task_id": str(task["id"]),
                    "graph_version": conditional_graph.GRAPH_VERSION,
                    "phase": "prepare",
                }
            }
        authorization = task["authorization_snapshot"]
        return {
            "data": {
                "task_id": str(task["id"]),
                "graph_version": GRAPH_VERSION,
                "phase": "prepare",
                "instruction": authorization["instruction"],
                "strategy": authorization.get("strategy", "hybrid"),
                "steps": 0,
                "messages": [],
                "queue": [],
                "observations": [],
                "action_index": 0,
            }
        }

    def run_once(self):
        task = self.claim()
        if task is None:
            return False
        pulse = _LeasePulse(self, task)
        pulse.start()
        services = None
        try:
            version = task["graph_version"]
            if version not in SUPPORTED_VERSIONS:
                raise CheckpointIncompatible(
                    f"Unsupported business graph version {version}; upgrade required"
                )
            conditional = version == conditional_graph.GRAPH_VERSION
            factory = self.conditional_services_factory if conditional else self.services_factory
            if factory is None:
                if conditional:
                    from .conditional_services import ConditionalBusinessServices

                    factory = ConditionalBusinessServices
                else:
                    from .services import BusinessServices

                    factory = BusinessServices

                services = factory(
                    self.database,
                    self.settings,
                    self.runtime_config,
                    task,
                    self.owner,
                    task["lease_generation"],
                    mcp_client=self._client(task),
                )
            else:
                services = factory(
                    self.database,
                    self.settings,
                    self.runtime_config,
                    task,
                    self.owner,
                    task["lease_generation"],
                )
            with self.database.checkpointer() as saver:
                graph_factory = (
                    self.conditional_graph_factory if conditional else self.graph_factory
                )
                graph = graph_factory(services, saver)
                config = {
                    "configurable": {"thread_id": str(task["id"])},
                    "recursion_limit": (
                        conditional_graph.RECURSION_LIMIT if conditional else MAX_STEPS + 8
                    ),
                }
                argument = self._input(graph, config, task)
                snapshot = graph.get_state(config)
                result = (
                    snapshot.values
                    if snapshot.values and not snapshot.next
                    else graph.invoke(argument, config)
                )
                data = result.get("data", {})
                pending = result.get("__interrupt__", ()) or self._interrupts(
                    graph.get_state(config)
                )
                phase = data.get("phase", task["phase"])
                phases = conditional_graph.PHASES if conditional else PHASES
                phase = phase if phase in phases else task["phase"]
                if pending:
                    value = pending[0].value
                    payload = (
                        dict(value) if isinstance(value, dict) else {"reason": "Input required"}
                    )
                    state = WAIT_STATES.get(payload.get("kind"), "awaiting_input")
                    payload.setdefault("reason", "Owner response is required before continuing")
                    payload.setdefault(
                        "question",
                        {
                            "paused_budget": "Which explicit budget should the task continue with?",
                            "recovering": "Request verification of the recorded external outcome.",
                            "awaiting_approval": "Approve, reject or edit this business action?",
                        }.get(state, "Provide the requested task clarification."),
                    )
                    self._transition(task, pulse, state, phase, "task.waiting", payload)
                elif (
                    data.get("reconciliation")
                    or (data.get("execution") or {}).get("status") == "unknown"
                ):
                    self._transition(
                        task,
                        pulse,
                        "recovering",
                        phase,
                        "task.waiting",
                        {
                            "kind": "reconciliation",
                            "reason": "Business result still requires independent reconciliation",
                            "question": "Reconcile the durable receipt and resource.",
                        },
                    )
                else:
                    completion = data.get("completion")
                    valid = isinstance(completion, dict) and completion.get("valid") is True
                    self._transition(
                        task,
                        pulse,
                        "succeeded" if valid else "failed",
                        "finalize",
                        "task.finished",
                        {"completion": completion or {"valid": False}},
                    )
        except CheckpointIncompatible as error:
            self._transition(
                task,
                pulse,
                "recovering",
                task["phase"],
                "task.waiting",
                {
                    "kind": "version",
                    "reason": str(error),
                    "question": "Upgrade and migrate this business checkpoint version.",
                },
            )
        except Exception as error:
            from decagent.business.mcp_client import MCPAuthenticationError

            if isinstance(error, MCPAuthenticationError):
                self._transition(
                    task,
                    pulse,
                    "awaiting_input",
                    task["phase"],
                    "task.waiting",
                    {
                        "kind": "employee_auth",
                        "reason": str(error),
                        "question": "请由原员工重新登录并恢复本任务的业务凭据。",
                    },
                )
            else:
                self._transition(
                    task,
                    pulse,
                    "failed",
                    task["phase"],
                    "task.failed",
                    {"error_type": type(error).__name__, "reason": str(error)},
                )
        finally:
            pulse.finish()
            if services is not None:
                services.close()
        return True

    def observe_merges(self):
        """Business workers have no repository merge maintenance."""
