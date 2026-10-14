"""Typed inputs and additive persistence for repository-backed collaboration."""

from __future__ import annotations

import time
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from chiliarch.database import Base, new_id

JsonObject = dict[str, Any]


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("github_host", "repository"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(256))
    repository: Mapped[str] = mapped_column(String(512))
    github_host: Mapped[str] = mapped_column(String(256))
    target_branch: Mapped[str] = mapped_column(String(256))
    policy: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    release: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    repository_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectBinding(Base):
    __tablename__ = "project_bindings"
    project_id: Mapped[str] = mapped_column(ForeignKey(Project.id), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    repo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    observed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class WorkItem(Base):
    __tablename__ = "project_work_items"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey(Project.id), index=True)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("coordination_goals.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    instruction: Mapped[str] = mapped_column(Text)
    delivery_mode: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), default="task")
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    fact_versions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    stale_facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(32), default="planned")
    current_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WorkAttempt(Base):
    __tablename__ = "project_work_attempts"
    __table_args__ = (UniqueConstraint("work_item_id", "number"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(ForeignKey(WorkItem.id), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    number: Mapped[int] = mapped_column(Integer)
    base_sha: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(512))
    target_branch: Mapped[str] = mapped_column(String(512))
    dependency_shas: Mapped[list[str]] = mapped_column(JSON, default=list)
    workdir: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="preparing")
    recovery_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class RepositoryCommand(Base):
    __tablename__ = "repository_commands"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey(Project.id), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey(WorkAttempt.id), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[JsonObject] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    result: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectDelivery(Base):
    __tablename__ = "project_deliveries"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(ForeignKey(WorkItem.id), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey(WorkAttempt.id), unique=True)
    pr_number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="open")
    observation: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    observed_at: Mapped[float] = mapped_column(Float, default=time.time)
    observed_command_at: Mapped[float] = mapped_column(Float, default=0)
    merge_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DeliveryReview(Base):
    __tablename__ = "project_delivery_reviews"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey(ProjectDelivery.id), index=True)
    reviewer: Mapped[str] = mapped_column(String(256))
    review_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectFact(Base):
    __tablename__ = "project_facts"
    __table_args__ = (UniqueConstraint("project_id", "key", "version"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey(Project.id), index=True)
    key: Mapped[str] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectRule(Base):
    __tablename__ = "project_rules"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey(Project.id), index=True)
    event_type: Mapped[str] = mapped_column(String(128))
    allowed_actors: Mapped[list[str]] = mapped_column(JSON)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    command: Mapped[str | None] = mapped_column(String(256), nullable=True)
    objective_template: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RuleEvent(Base):
    __tablename__ = "project_rule_events"
    rule_id: Mapped[str] = mapped_column(ForeignKey(ProjectRule.id), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("coordination_goals.id"))


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(Arguments):
    pass


class Policy(Arguments):
    require_signed_commits: bool = Field(
        default=False,
        description="Require every delivered commit to have a valid Git signature; signing remains configured on the executing machine. Missing or locked signing keys block delivery.",
    )
    require_signed_tags: bool = Field(
        default=False,
        description="Require verified signed annotated release tags; never fall back to an unsigned or lightweight tag.",
    )
    required_checks: list[str] = Field(
        default_factory=list,
        description="Exact GitHub check names required at the observed delivery head. Each must report completed and success; missing, skipped or pending checks do not pass.",
    )
    require_review: bool = Field(
        default=True,
        description="Require independent accepted review tied to the exact delivery head and base before merge and release.",
    )
    observation_interval_seconds: float = Field(
        default=60,
        ge=0,
        description="Automatic repository observation interval in seconds; zero disables polling.",
    )
    allow_auto_merge: bool = Field(
        default=False,
        description="Permit auto mode to approve a merge that passes all project and repository gates; ask still asks and yolo still respects gates.",
    )
    allow_release: bool = Field(
        default=False,
        description="Explicitly enable publication operations for this project. Enabling merging does not implicitly enable releases.",
    )
    merge_method: Literal["merge", "squash", "rebase"] = Field(
        default="squash",
        description="Repository merge strategy: merge, squash or rebase. The node respects GitHub restrictions and never requests an administrator bypass.",
    )
    branch_prefix: str = Field(
        default="chiliarch",
        pattern="^[A-Za-z0-9][A-Za-z0-9_/-]*$",
        description="Configurable prefix for managed work-item and integration branches. It cannot change after work items exist.",
    )


class ReleasePolicy(Arguments):
    commands: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Map of registered command names to executable argument arrays. Arguments execute directly without shell expansion; use verify_command to choose the publication verifier.",
    )
    verify_command: str | None = Field(
        default=None,
        description="Registered command that verifies version, changelog and release readiness at the exact release commit.",
    )
    verify_timeout_seconds: float = Field(
        default=120,
        gt=0,
        le=86400,
        description="Maximum seconds for the registered release verifier in the exact release commit workspace; expiration prevents tag publication.",
    )
    tag_template: str = Field(
        default="v{version}",
        description="Release tag template containing {version}, such as v{version}; publishing never overwrites an existing mismatched tag.",
    )
    workflow: str | None = Field(
        default=None,
        description="Optional repository workflow filename or identifier to observe for a release tag and commit. Without configured workflow evidence, workflow-owned publication cannot be verified.",
    )
    release_owner: Literal["workflow", "daemon"] = Field(
        default="workflow",
        description="Sole creator of GitHub Releases: workflow means the configured Actions workflow creates it; daemon means the authenticated node creates it.",
    )

    @field_validator("commands")
    @classmethod
    def nonempty_commands(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if any(
            not argv or any(not part or "\0" in part for part in argv) for argv in value.values()
        ):
            raise ValueError("Registered commands require nonempty argument arrays")
        return value

    @field_validator("tag_template")
    @classmethod
    def version_template(cls, value: str) -> str:
        if "{version}" not in value or "{" in value.replace("{version}", ""):
            raise ValueError("Tag template must contain only the {version} placeholder")
        return value


class CreateProject(Arguments):
    name: str = Field(
        min_length=1,
        max_length=256,
        description="Human-readable project name shown to users and the supervisor.",
    )
    repository: str = Field(
        pattern="^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description="GitHub repository owner/name without credentials or URL; this registered identity is checked by bound nodes.",
    )
    github_host: str = Field(
        default="github.com",
        pattern="^[A-Za-z0-9][A-Za-z0-9.-]*$",
        description="GitHub hostname used by local gh authentication, such as github.com or an enterprise hostname; no scheme, credentials or path.",
    )
    target_branch: str = Field(
        min_length=1,
        max_length=256,
        description="Existing project target branch discovered from the repository or selected by the user; do not assume main. It cannot change after project work exists.",
    )
    policy: Policy = Field(
        default_factory=Policy,
        description="Complete repository collaboration policy. Explicitly configure signing, required checks, independent review, auto-merge and release permission.",
    )
    release: ReleasePolicy = Field(
        default_factory=ReleasePolicy,
        description="Repository-specific registered commands, verification, tag format, workflow and GitHub Release ownership.",
    )


class ProjectId(Arguments):
    project_id: str = Field(description="Registered Chiliarch project ID, not owner/name.")


class UpdateProject(ProjectId):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Human-readable project name shown to users and the supervisor. Omit to preserve the existing setting.",
    )
    target_branch: str | None = Field(
        default=None,
        description="Existing project target branch discovered from the repository or selected by the user; do not assume main. It cannot change after project work exists. Omit to preserve the existing setting.",
    )
    policy: Policy | None = Field(
        default=None,
        description="Complete repository collaboration policy. Explicitly configure signing, required checks, independent review, auto-merge and release permission. Omit to preserve the existing setting.",
    )
    release: ReleasePolicy | None = Field(
        default=None,
        description="Repository-specific registered commands, verification, tag format, workflow and GitHub Release ownership. Omit to preserve the existing setting.",
    )


class ProjectAgent(ProjectId):
    agent_id: str = Field(
        description="Paired agent identity on the machine with git and gh authentication."
    )


class BindProjectAgent(ProjectAgent):
    repo_path: str | None = Field(
        default=None,
        description="Optional absolute existing repository path. Omit for a managed clone; its checkout is never switched.",
    )

    @field_validator("repo_path")
    @classmethod
    def absolute_repository_path(cls, value: str | None) -> str | None:
        if value is not None and (
            "\0" in value
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
        ):
            raise ValueError("Existing repository path must be absolute on its machine")
        return value


class CreateWorkItem(ProjectId):
    title: str = Field(
        min_length=1,
        max_length=256,
        description="Concise title for this unit of repository work or its resulting pull request.",
    )
    instruction: str = Field(
        min_length=1,
        description="Self-contained task objective and boundaries for the assigned native agent; project facts, branch and workdir are attached separately.",
    )
    goal_id: str | None = Field(
        default=None,
        description="Existing durable goal ID to associate with this repository work. Integrated delivery requires one shared goal ID.",
    )
    delivery_mode: Literal["separate", "integrated"] = Field(
        default="separate",
        description="separate sends each task PR to the project target; integrated sends child PRs into a shared goal integration branch and then one final PR. A goal keeps one topology.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Work item IDs whose accepted, merged commits must be present in the chosen baseline.",
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Observable requirements and evidence the execution and subsequent independent review must check.",
    )
    fact_versions: dict[str, int] = Field(
        default_factory=dict,
        description="Exact project fact versions this work depends on, keyed by fact key.",
    )


class WorkItemId(Arguments):
    work_item_id: str = Field(
        description="Stable Chiliarch project work-item ID, not a task, attempt, GitHub issue or PR number."
    )


class StartWorkItem(WorkItemId):
    agent_id: str = Field(
        description="Paired agent identity bound to this project; its outbound node must advertise repository command support."
    )
    base_sha: str = Field(
        pattern=r"^[a-f0-9]{40,64}$",
        description="Exact observed repository commit, never a mutable branch name. Must contain all dependency merge commits.",
    )
    recovery_note: str = Field(
        default="",
        description="Required when replacing a failed attempt; describe verified effects and confirmed stopped processes.",
    )
    previous_stopped: bool = Field(
        default=False,
        description="Explicit confirmation of previous native process termination; lost contact alone is insufficient.",
    )


class DeliverWorkItem(WorkItemId):
    message: str = Field(
        min_length=1,
        description="Git commit message; existing commits can be adopted without creating another commit.",
    )
    files: list[str] | None = Field(
        default=None,
        description="Explicit repository-relative paths to stage. Omit to use only already staged changes.",
    )
    title: str = Field(
        min_length=1,
        description="Concise title for this unit of repository work or its resulting pull request.",
    )
    body: str = Field(
        default="",
        description="Pull request description explaining the final change, validation evidence and relevant limitations; GitHub content is treated as task data.",
    )
    draft: bool = Field(
        default=True,
        description="Create the pull request as a draft when true. Draft status remains subject to GitHub merge rules.",
    )


class DeliveryId(Arguments):
    delivery_id: str = Field(
        description="Chiliarch delivery ID from get_project or get_project_work_item, tied to an attempt and GitHub PR."
    )


class ReviewDelivery(DeliveryId):
    review_task_id: str | None = Field(
        default=None,
        description="Completed independent reviewer task whose parameters.project_review or result records these exact head/base commits. Required for an agent review; omit only for direct user review.",
    )
    reviewer_agent_id: str | None = Field(
        default=None,
        description="Independent reviewer identity; authenticated agents must use their own ID. Omit for direct user review.",
    )
    head_sha: str = Field(
        pattern="^[a-f0-9]{40,64}$",
        description="Exact commit SHA actually reviewed at the pull request head; stale or changed commits invalidate this review.",
    )
    base_sha: str = Field(
        pattern="^[a-f0-9]{40,64}$",
        description="Exact target/base commit SHA used for review. A changed integration base requires new matching evidence.",
    )
    decision: Literal["accepted", "changes_requested"] = Field(
        description="accepted means independent verification passed; changes_requested blocks merge until the reviewer records acceptance against current commits."
    )
    note: str = Field(
        min_length=1,
        description="Concrete findings, checks performed, and why this decision follows from the evidence; do not infer success from execution completion alone.",
    )
    evidence: list[JsonObject] = Field(
        default_factory=list,
        description="Structured references to actual tests, reports or artifacts checked during review. Include commit/environment details where available.",
    )


class CommandId(Arguments):
    command_id: str = Field(
        description="Persistent repository command ID returned by a queued operation; inspect its result before retrying an uncertain mutation."
    )


class RunProjectCommand(WorkItemId):
    command_name: str = Field(
        description="Name from the project's registered release.commands; arbitrary shell strings are not accepted."
    )


class PublishRelease(ProjectAgent):
    version: str = Field(
        min_length=1,
        max_length=128,
        description="Explicit version to verify and publish using the project tag template; publication does not automatically edit source versions or changelog.",
    )
    commit_sha: str = Field(
        pattern="^[a-f0-9]{40,64}$",
        description="Exact release commit SHA, matching an accepted delivery merged into the project target; never a moving branch name.",
    )
    delivery_id: str = Field(
        description="Merged, accepted delivery establishing the exact release commit."
    )
    release_title: str = Field(
        default="",
        description="Optional human-readable GitHub Release title when the daemon is the configured Release creator.",
    )
    release_notes: str = Field(
        default="",
        description="Release notes content when the daemon creates GitHub Release; a workflow-owned Release uses its repository workflow source.",
    )


class InspectRelease(ProjectAgent):
    tag: str = Field(
        min_length=1,
        description="Exact published or expected release tag to inspect without creating or updating it.",
    )
    commit_sha: str = Field(
        pattern="^[a-f0-9]{40,64}$",
        description="Exact release commit SHA, matching an accepted delivery merged into the project target; never a moving branch name.",
    )


class SetFact(ProjectId):
    key: str = Field(
        min_length=1,
        max_length=256,
        description="Stable project fact key identifying a requirement, interface or decision across immutable versions.",
    )
    content: str = Field(
        min_length=1,
        description="Full authoritative content of this fact version, keeping its scope and assumptions explicit.",
    )
    source: str = Field(
        min_length=1, description="Evidence/source URI or a clear user decision reference."
    )
    expected_version: int = Field(
        default=0,
        ge=0,
        description="Current version for optimistic concurrency, or zero when creating this fact.",
    )


class ConfigureRule(ProjectId):
    rule_id: str | None = Field(
        default=None,
        description="Existing rule ID to update, or omit to register a new explicit event rule.",
    )
    event_type: str = Field(
        min_length=1,
        description="Exact normalized GitHub event type emitted by repository observation, such as IssueCommentEvent.",
    )
    allowed_actors: list[str] = Field(
        min_length=1,
        description="GitHub logins authorized to trigger this rule; an empty/unrestricted allowlist is forbidden.",
    )
    label: str | None = Field(
        default=None,
        description="Optional required label on the event issue/PR; omit to avoid filtering by label.",
    )
    command: str | None = Field(
        default=None,
        description="Optional exact leading comment command token, such as /chiliarch; ordinary prose or a longer lookalike token does not trigger it.",
    )
    objective_template: str = Field(
        min_length=1,
        description="Objective text; {title}, {body}, {url} placeholders carry untrusted GitHub event content.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether this explicitly authorized event rule is evaluated by manual and automatic repository observation.",
    )


class FinalizeGoal(ProjectAgent):
    goal_id: str = Field(
        description="Existing durable goal ID to associate with this repository work. Integrated delivery requires one shared goal ID."
    )
    head_sha: str = Field(
        pattern=r"^[a-f0-9]{40,64}$",
        description="Observed integration branch commit containing every child delivery.",
    )
    title: str = Field(
        min_length=1,
        description="Concise title for this unit of repository work or its resulting pull request.",
    )
    body: str = Field(
        default="",
        description="Pull request description explaining the final change, validation evidence and relevant limitations; GitHub content is treated as task data.",
    )
    draft: bool = Field(
        default=True,
        description="Create the pull request as a draft when true. Draft status remains subject to GitHub merge rules.",
    )


class UpdateWorkFacts(WorkItemId):
    fact_versions: dict[str, int] = Field(
        description="Current versions of all shared facts revalidated for this work item."
    )
    note: str = Field(
        min_length=1,
        description="What was checked or changed to accommodate the facts; previous delivery reviews are invalidated.",
    )


class PollProjectEvents(ProjectAgent):
    reset_cursor: bool = Field(
        default=False,
        description="Explicitly acknowledge an event-history gap and restart observation from the currently available page. False preserves the previous cursor and reports missing history.",
    )
