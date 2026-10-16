"""Closed input DTOs; provenance and operative snapshots never come from requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInput(Input):
    token: str = Field(min_length=32, max_length=512, repr=False)


class EmployeeLoginInput(Input):
    login: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    password: str = Field(min_length=1, max_length=512, repr=False)


class BusinessOperationReplyInput(Input):
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject", "edit"]
    edited_params: dict | None = None


class ProjectInput(Input):
    repository: str = Field(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", max_length=200)
    base_branch: str = Field(default="main", min_length=1, max_length=200)


class BudgetInput(Input):
    model_budget_version: Literal["1"] = "1"
    primary_target: int = Field(default=3, ge=0, le=100000)
    max_primary_calls: int = Field(default=6, ge=0, le=100000)
    max_jev_calls: int = Field(default=24, ge=0, le=100000)
    max_calls: int = Field(default=30, ge=1, le=100000)
    max_tokens: int = Field(default=100000, ge=1, le=100000000)
    max_active_seconds: int = Field(default=1800, ge=1, le=86400)
    max_cost: str | None = Field(default=None, max_length=30)
    allow_unknown_price: bool = False
    max_actions: int = Field(default=128, ge=1, le=100000)
    max_plan_revisions: int = Field(default=4, ge=1, le=4)
    max_node_attempts: int = Field(default=128, ge=1, le=100000)
    max_graph_transitions: int = Field(default=1024, ge=1, le=100000)

    @model_validator(mode="after")
    def consistent_primary_target(self):
        if self.primary_target > self.max_primary_calls:
            raise ValueError("Primary target exceeds the task's hard primary quota")
        return self


class TrustInput(Input):
    kind: Literal["exact_commit", "own_branch"] = "exact_commit"
    branch: str | None = Field(default=None, max_length=200)


class TaskInput(Input):
    project_id: str
    base_ref: str = Field(min_length=1, max_length=200)
    issue: str = Field(min_length=1, max_length=65536)
    trust_scope: TrustInput | None = None
    budget: BudgetInput = Field(default_factory=BudgetInput)
    publish_draft_pr: bool = False


class WorkflowSelectionInput(Input):
    workflow_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    goal_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str | None = Field(default=None, min_length=1, max_length=200)
    inputs: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def explicit_target(self):
        from decagent.decisions.contracts import canonical_json

        if bool(self.workflow_id) == bool(self.goal_id):
            raise ValueError("Select exactly one registered workflow or goal")
        if self.goal_id and self.version is not None:
            raise ValueError("A goal match cannot assume a workflow version")
        if self.workflow_id and (self.version is None or not self.version.strip()):
            raise ValueError("An explicit workflow requires its published version")
        if "instruction" in self.inputs or len(canonical_json(self.inputs).encode()) > 65536:
            raise ValueError(
                "Workflow inputs cannot replace the original instruction or exceed bounds"
            )
        return self


class BusinessTaskInput(Input):
    scope_id: str = Field(min_length=36, max_length=36)
    instruction: str = Field(min_length=1, max_length=65536)
    strategy: Literal["rule", "main", "hybrid"] = "hybrid"
    budget: BudgetInput = Field(default_factory=BudgetInput)
    workflow: WorkflowSelectionInput | None = None


class ResumeInput(Input):
    budget: BudgetInput | None = None


class ReplyInput(Input):
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject", "edit", "confirm"]
    edited_params: dict | None = None
    confirmation: str | None = Field(default=None, min_length=1, max_length=65536)


class ResponseInput(Input):
    question_id: str
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(min_length=1, max_length=65536)


class DiagnosticInput(Input):
    mode: Literal["local", "live"] = "local"
    target: Literal["main", "jev", "github", "executor"] | None = None
    confirm_external_request: bool = False


class MemoryReviewInput(Input):
    operation: Literal["activate", "reject"]
    confirmation_id: str | None = None
    confirmation: str | None = Field(default=None, min_length=1, max_length=65536)
    expected_revision: int = Field(ge=1)


class MemoryRevokeInput(Input):
    expected_revision: int = Field(ge=1)


class SemanticReplyInput(Input):
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_plan_revision: int = Field(ge=1, le=4)
    decision: Literal["select", "no_match", "supplement", "reject"]
    value: str | bool | int | None = None
    text: str | None = Field(default=None, min_length=1, max_length=65536)

    @model_validator(mode="after")
    def semantic_shape(self):
        if self.decision == "select":
            if self.value is None or self.text is not None:
                raise ValueError("A semantic selection requires exactly one typed value")
        elif self.value is not None:
            raise ValueError("Only a selection may contain a value")
        if self.decision == "supplement" and (not self.text or not self.text.strip()):
            raise ValueError("A supplement requires concrete facts")
        if self.decision == "no_match" and self.text is not None:
            raise ValueError("No-match is distinct from supplying additional facts")
        return self


class DecisionThresholdInput(Input):
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    policy_version: str = Field(min_length=1, max_length=200)
    threshold: float = Field(ge=0, le=1)


class BusinessMemoryConfirmInput(Input):
    task_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class BusinessMemoryCorrectionInput(BusinessMemoryConfirmInput):
    value: str = Field(min_length=1, max_length=2048)


class BusinessMemoryRevocationInput(BusinessMemoryConfirmInput):
    reason: str = Field(min_length=1, max_length=512)


class BusinessMemorySourceRevocationInput(Input):
    task_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    reference: dict
    reason: str = Field(min_length=1, max_length=512)
