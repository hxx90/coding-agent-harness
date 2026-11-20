from __future__ import annotations

from datetime import datetime
from enum import StrEnum, auto
from typing import Annotated, ClassVar, Literal, Self, cast

from pydantic import BaseModel, Field, JsonValue, model_validator

from vibe.app_server._model import ProtocolModel
from vibe.questions import UserQuestionRequest
from vibe.utils.tool_presentation import EffectCallDisplay, ToolEffectKind

# The ``tool_name`` on the effect behind a manual `!<command>`. A model's shell
# call carries the real tool's name (``bash``, ``git_bash``, ``powershell``), so
# this is what tells a user's own shell output apart from the agent's -- the
# ``SHELL`` effect kind alone covers both.
MANUAL_SHELL_TOOL_NAME = "shell"


class ShellEffectInput(ProtocolModel):
    command: str


class ShellEffectOutput(ProtocolModel):
    stdout: str
    stderr: str
    # Arrival-ordered transcript; empty for producers that never streamed.
    output: str = ""
    truncated: bool = False

    @property
    def transcript(self) -> str:
        if self.output:
            return self.output
        # Separate captures: concatenating them bare would fabricate a line.
        if self.stdout and self.stderr and not self.stdout.endswith("\n"):
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout + self.stderr


class FileEditEffectInput(ProtocolModel):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class FileEditEffectChange(ProtocolModel):
    old_string: str
    new_string: str
    replace_all: bool = False


class FileEditEffectBatchInput(ProtocolModel):
    file_path: str
    changes: list[FileEditEffectChange] = Field(min_length=1)


class FileEditEffectOccurrence(ProtocolModel):
    start_line: int | None = None
    old_text: str
    new_text: str


class FileEditEffectOutput(ProtocolModel):
    file: str
    old_string: str | None = None
    new_string: str | None = None
    occurrences: list[FileEditEffectOccurrence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_diff_content(self) -> Self:
        has_legacy_pair = self.old_string is not None and self.new_string is not None
        if (self.old_string is None) != (self.new_string is None):
            raise ValueError("old_string and new_string must be provided together")
        if not has_legacy_pair and not self.occurrences:
            raise ValueError("file edit output requires a diff pair or occurrences")
        return self


class FileSearchEffectInput(ProtocolModel):
    pattern: str
    path: str = "."
    max_matches: int | None = None


class FileSearchEffectMatch(ProtocolModel):
    path: str
    line: int | None = None


class FileSearchEffectOutput(ProtocolModel):
    matches: str
    match_count: int
    was_truncated: bool
    parsed_matches: list[FileSearchEffectMatch] = Field(default_factory=list)


class FileReadEffectInput(ProtocolModel):
    DEFAULT_LIMIT: ClassVar[int] = 2000

    file_path: str
    offset: int | None = None
    limit: int | None = DEFAULT_LIMIT


class FileReadEffectOutput(ProtocolModel):
    file_path: str
    content: str
    num_lines: int
    start_line: int
    requested_offset: int | None = None
    requested_limit: int | None = FileReadEffectInput.DEFAULT_LIMIT
    total_lines: int | None = None
    was_truncated: bool = False


class TodoEffectStatus(StrEnum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    CANCELLED = auto()


class TodoEffectPriority(StrEnum):
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()


class TodoEffectItem(ProtocolModel):
    id: str
    content: str
    status: TodoEffectStatus = TodoEffectStatus.PENDING
    priority: TodoEffectPriority = TodoEffectPriority.MEDIUM


class TodoEffectInput(ProtocolModel):
    action: str
    todos: list[TodoEffectItem] | None = None


class TodoEffectOutput(ProtocolModel):
    todos: list[TodoEffectItem] = Field(default_factory=list)


class FileWriteEffectInput(ProtocolModel):
    file_path: str
    content: str


class FileWriteEffectOutput(ProtocolModel):
    file_path: str
    content: str


class WebSearchEffectInput(ProtocolModel):
    query: str


class WebSearchEffectSource(ProtocolModel):
    title: str
    url: str


class WebSearchEffectOutput(ProtocolModel):
    query: str
    answer: str
    sources: list[WebSearchEffectSource] = Field(default_factory=list)


class WebFetchEffectInput(ProtocolModel):
    url: str
    timeout: int | None = None


class WebFetchEffectOutput(ProtocolModel):
    url: str
    content: str
    content_type: str
    was_truncated: bool = False


class SkillEffectInput(ProtocolModel):
    name: str


class SkillEffectOutput(ProtocolModel):
    name: str
    content: str
    skill_dir: str | None = None


class SubagentEffectInput(ProtocolModel):
    task: str
    agent: str


class SubagentEffectOutput(ProtocolModel):
    response: str
    turns_used: int
    completed: bool


# The path is carried alongside the name because a managed worktree lives under
# $VIBE_HOME rather than beside the repo, so the name alone does not tell the
# user where on disk the directory landed.
class WorktreeEffectInput(ProtocolModel):
    name: str
    branch: str
    path: str


class HardwareActionInput(ProtocolModel):
    action: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    command_id: str | None = None


class HardwareEffectInput(ProtocolModel):
    action: str | None = None
    device_id: str | None = None
    lease_id: str | None = None
    command_id: str | None = None
    owner: str | None = None
    ttl_seconds: float | None = None
    parameters: dict[str, JsonValue] | None = None
    actions: list[HardwareActionInput] | None = None
    criterion: str | None = None
    reason: str | None = None
    run_id: str | None = None
    include_workspace: bool | None = None
    code: str | None = None


class HardwareActionCapabilityOutput(ProtocolModel):
    name: str
    description: str
    parameters_schema: dict[str, JsonValue] = Field(default_factory=dict)
    destructive: bool = True


class HardwareVerificationCapabilityOutput(ProtocolModel):
    name: str
    description: str
    parameters_schema: dict[str, JsonValue] = Field(default_factory=dict)
    applicable_actions: list[str] = Field(default_factory=list)
    parameter_bindings: dict[str, str] = Field(default_factory=dict)


class HardwareDeviceOutput(ProtocolModel):
    schema_version: str = "1.0"
    device_id: str
    adapter_id: str | None = None
    manufacturer: str
    model: str
    kind: str
    transport: str
    actions: list[HardwareActionCapabilityOutput] = Field(default_factory=list)
    verifications: list[HardwareVerificationCapabilityOutput] = Field(
        default_factory=list
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class HardwareEvidenceOutput(ProtocolModel):
    evidence_id: str | None = None
    source_id: str
    modality: str | None = None
    captured_at: datetime | None = None
    sequence: int | None = None
    mime_type: str | None = None
    uri: str
    width: int | None = None
    height: int | None = None


class HardwareSnapshotOutput(ProtocolModel):
    device_id: str
    status: str
    sequence: int
    observed_at: datetime | None = None
    state: dict[str, JsonValue] = Field(default_factory=dict)
    faults: list[str] = Field(default_factory=list)
    evidence: list[HardwareEvidenceOutput] = Field(default_factory=list)


class HardwareLeaseOutput(ProtocolModel):
    lease_id: str
    device_id: str | None = None
    owner: str | None = None
    acquired_at: datetime | None = None
    expires_at: datetime


class HardwareCommandReceiptOutput(ProtocolModel):
    command_id: str
    device_id: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


class HardwareStopReceiptOutput(ProtocolModel):
    device_id: str
    acknowledged: bool
    observed_at: datetime | None = None
    message: str


class HardwareTraceEventOutput(ProtocolModel):
    sequence: int
    run_id: str | None = None
    kind: str
    occurred_at: datetime | None = None
    device_id: str | None = None
    command_id: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class HardwareTraceOutput(ProtocolModel):
    run_id: str
    events: list[HardwareTraceEventOutput] = Field(default_factory=list)


class HardwareWorkspaceOutput(ProtocolModel):
    cwd: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)


class HardwareVerificationCheckOutput(ProtocolModel):
    name: str
    status: str
    observed: JsonValue = None
    expected: str
    evidence_ids: list[str] = Field(default_factory=list)


class HardwareVerificationOutput(ProtocolModel):
    verification_id: str | None = None
    device_id: str | None = None
    criterion: str
    status: str
    observed_at: datetime | None = None
    snapshot_sequence: int | None = None
    checks: list[HardwareVerificationCheckOutput] = Field(default_factory=list)
    summary: str
    evidence: list[HardwareEvidenceOutput] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class HardwarePolicyStepOutput(ProtocolModel):
    sequence: int
    kind: str
    name: str
    line: int
    output: JsonValue = None


class HardwarePolicyOutput(ProtocolModel):
    geometry_library: str = "off"
    policy_id: str
    source: str
    source_sha256: str
    source_uri: str
    status: str
    steps: list[HardwarePolicyStepOutput] = Field(default_factory=list)
    stdout: list[str] = Field(default_factory=list)
    receipts: list[HardwareCommandReceiptOutput] = Field(default_factory=list)
    snapshot: HardwareSnapshotOutput
    report: HardwareVerificationOutput | None = None
    task_outcome: str


class HardwareEffectOutput(ProtocolModel):
    action: str | None = None
    devices: list[HardwareDeviceOutput] | None = None
    snapshot: HardwareSnapshotOutput | None = None
    lease: HardwareLeaseOutput | None = None
    released: bool | None = None
    receipt: HardwareCommandReceiptOutput | HardwareStopReceiptOutput | None = None
    receipts: list[HardwareCommandReceiptOutput] | None = None
    task_outcome: str | None = None
    trace: HardwareTraceOutput | None = None
    workspace: HardwareWorkspaceOutput | None = None
    report: HardwareVerificationOutput | None = None
    policy: HardwarePolicyOutput | None = None


class _EffectDetailBase(ProtocolModel):
    tool_name: str
    display: EffectCallDisplay


class GenericEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.TOOL] = ToolEffectKind.TOOL
    input: JsonValue = None


class ShellEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.SHELL] = ToolEffectKind.SHELL
    input: ShellEffectInput | None = None


class FileEditEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.FILE_EDIT] = ToolEffectKind.FILE_EDIT
    input: FileEditEffectInput | FileEditEffectBatchInput | None = None


class FileSearchEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.FILE_SEARCH] = ToolEffectKind.FILE_SEARCH
    input: FileSearchEffectInput | None = None


class FileReadEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.FILE_READ] = ToolEffectKind.FILE_READ
    input: FileReadEffectInput | None = None


class TodoEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.TODO] = ToolEffectKind.TODO
    input: TodoEffectInput | None = None


class FileWriteEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.FILE_WRITE] = ToolEffectKind.FILE_WRITE
    input: FileWriteEffectInput | None = None


class UserQuestionEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.USER_QUESTION] = ToolEffectKind.USER_QUESTION
    input: UserQuestionRequest | None = None


class WebSearchEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.WEB_SEARCH] = ToolEffectKind.WEB_SEARCH
    input: WebSearchEffectInput | None = None


class WebFetchEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.WEB_FETCH] = ToolEffectKind.WEB_FETCH
    input: WebFetchEffectInput | None = None


class SkillEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.SKILL] = ToolEffectKind.SKILL
    input: SkillEffectInput | None = None


class SubagentEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.SUBAGENT] = ToolEffectKind.SUBAGENT
    input: SubagentEffectInput | None = None
    child_session_id: str | None = None


class WorktreeEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.WORKTREE] = ToolEffectKind.WORKTREE
    input: WorktreeEffectInput | None = None


class ProcessEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.PROCESS] = ToolEffectKind.PROCESS
    input: JsonValue = None


class HardwareEffectDetail(_EffectDetailBase):
    kind: Literal[ToolEffectKind.HARDWARE] = ToolEffectKind.HARDWARE
    input: HardwareEffectInput | None = None


EffectDetail = Annotated[
    GenericEffectDetail
    | ShellEffectDetail
    | FileEditEffectDetail
    | FileSearchEffectDetail
    | FileReadEffectDetail
    | TodoEffectDetail
    | FileWriteEffectDetail
    | UserQuestionEffectDetail
    | WebSearchEffectDetail
    | WebFetchEffectDetail
    | SkillEffectDetail
    | SubagentEffectDetail
    | WorktreeEffectDetail
    | ProcessEffectDetail
    | HardwareEffectDetail,
    Field(discriminator="kind"),
]


def effect_input_json(detail: EffectDetail) -> JsonValue:
    value = detail.input
    if not isinstance(value, BaseModel):
        return value
    return cast(JsonValue, value.model_dump(mode="json", by_alias=True))
