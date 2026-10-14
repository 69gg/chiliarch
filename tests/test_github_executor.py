"""Observable Git/GitHub effects, crash recovery and exact revision contracts."""

# ruff: noqa: ASYNC240 -- Tiny synchronous fixture files are intentional.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from chiliarch.github_executor import CommandResult, GitHubExecutor

BASE = "a" * 40
HEAD = "b" * 40
OTHER = "c" * 40
REPOSITORY = {"host": "forge.example", "owner": "team", "name": "project", "repository_id": 17}
COMMON = {"project_id": "project-1", "repository": REPOSITORY, "policy": {}}


def command(action: str, identifier: str = "operation", **arguments: Any) -> dict[str, Any]:
    if action == "publish_tag":
        arguments = {"version": "1.0.0", "verify_argv": ["fixture-check", "{version}"], **arguments}
    return {"command_id": identifier, "action": action, "arguments": {**COMMON, **arguments}}


class FakeGitHub:
    """A remote state model; failures can happen after a mutation takes effect."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.refs: dict[str, str] = {}
        self.workspaces: dict[Path, dict[str, str]] = {}
        self.prs: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.staged: list[str] = []
        self.dirty = ""
        self.push_disconnect = False
        self.create_disconnect = False
        self.merge_disconnect = False
        self.comment_disconnect = False
        self.issue_disconnect = False
        self.issues: dict[int, dict[str, Any]] = {}
        self.signature_valid = True
        self.dependencies_valid = True
        self.remote_url = "https://forge.example/team/project.git"
        self.repository_id = 17
        self.created_bodies: list[str] = []
        self.local_tags: dict[str, str] = {}
        self.ancestor_status = "ahead"
        self.verifier_exit = 0
        self.verifier_commit: str | None = None
        self.checks: list[dict[str, Any]] = [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ]

    @staticmethod
    def json(value: Any) -> CommandResult:
        return CommandResult(0, json.dumps(value))

    def make_pr(
        self, branch: str = "chiliarch/work/a1", base: str = "main", head: str = HEAD
    ) -> dict[str, Any]:
        value = {
            "number": len(self.prs) + 1,
            "url": "https://forge.example/team/project/pull/1",
            "headRefOid": head,
            "baseRefOid": BASE,
            "headRefName": branch,
            "baseRefName": base,
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
            "statusCheckRollup": self.checks,
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
        }
        self.prs.append(value)
        return value

    async def __call__(self, argv: list[str], cwd: Path) -> CommandResult:
        self.calls.append((argv[:], cwd))
        if argv[0] == "git":
            args = argv[1:]
            if args == ["--version"]:
                return CommandResult(0, "git version fixture")
            if args[:1] == ["config"]:
                return CommandResult(1)
            if args[:1] == ["check-ref-format"]:
                return CommandResult(int(".." in args[-1] or " " in args[-1]))
            if args == ["remote", "get-url", "origin"]:
                return CommandResult(0, self.remote_url)
            if args[:1] == ["fetch"]:
                return CommandResult(0)
            if args[:2] == ["merge-base", "--is-ancestor"]:
                return CommandResult(0 if self.dependencies_valid else 1)
            if args[:2] == ["worktree", "add"]:
                path = Path(args[-2])
                path.mkdir(parents=True)
                self.workspaces[path] = {
                    "branch": "" if "--detach" in args else args[3],
                    "head": args[-1],
                }
                return CommandResult(0)
            if args[:2] == ["worktree", "remove"]:
                Path(args[-1]).rmdir()
                del self.workspaces[Path(args[-1])]
                return CommandResult(0)
            if args == ["rev-parse", "--show-toplevel"]:
                return CommandResult(0, str(cwd))
            if args == ["branch", "--show-current"]:
                return CommandResult(0, self.workspaces[cwd]["branch"])
            if args == ["rev-parse", "HEAD"]:
                return CommandResult(0, self.workspaces[cwd]["head"])
            if args[:2] == ["rev-parse", "--verify"]:
                if args[-1].startswith("refs/tags/"):
                    tag = args[-1].removeprefix("refs/tags/").removesuffix("^{commit}")
                    return (
                        CommandResult(0, self.local_tags[tag])
                        if tag in self.local_tags
                        else CommandResult(1)
                    )
                return CommandResult(0, args[-1].removesuffix("^{commit}"))
            if args == ["diff", "--cached", "--name-only", "-z"]:
                return CommandResult(0, "\0".join(self.staged) + ("\0" if self.staged else ""))
            if args == ["diff", "--cached", "--quiet"]:
                return CommandResult(int(bool(self.staged)))
            if args == ["diff", "--quiet", "HEAD", "--"]:
                return CommandResult(0)
            if args[:2] == ["--literal-pathspecs", "add"]:
                self.staged.extend(args[3:])
                return CommandResult(0)
            if args[:1] == ["commit"]:
                if "-S" in args and not self.signature_valid:
                    return CommandResult(1, stderr="private signing details should stay local")
                self.workspaces[cwd]["head"] = HEAD
                self.staged.clear()
                return CommandResult(0)
            if args[:1] in (["verify-commit"], ["verify-tag"]):
                return CommandResult(0 if self.signature_valid else 1)
            if args[:1] == ["rev-list"]:
                return CommandResult(0, HEAD + "\n")
            if args[:1] == ["push"]:
                source, target = args[-1].split(":", 1)
                self.refs[target] = (
                    self.local_tags[source.removeprefix("refs/tags/")]
                    if source.startswith("refs/tags/")
                    else source
                )
                if self.push_disconnect:
                    raise ConnectionError("result lost after successful push")
                return CommandResult(0)
            if args[:1] == ["tag"]:
                self.local_tags[args[2]] = args[3]
                return CommandResult(0)
            if args[:1] == ["status"]:
                return CommandResult(0, self.dirty)
        if argv[:2] == ["gh", "--version"]:
            return CommandResult(0, "gh version fixture")
        if argv[:3] == ["gh", "repo", "clone"]:
            Path(argv[4]).mkdir(parents=True)
            return CommandResult(0)
        if argv[:2] == ["gh", "api"]:
            endpoint = unquote(argv[4])
            if endpoint == "user":
                return self.json({"login": "fixture"})
            if endpoint == "repos/team/project":
                return self.json(
                    {
                        "id": self.repository_id,
                        "default_branch": "main",
                        "permissions": {"push": True},
                    }
                )
            if "/git/matching-refs/" in endpoint:
                prefix = "refs/" + endpoint.split("/git/matching-refs/", 1)[1]
                return self.json(
                    [
                        {"ref": ref, "object": {"type": "commit", "sha": sha}}
                        for ref, sha in self.refs.items()
                        if ref.startswith(prefix)
                    ]
                )
            if "/comments?" in endpoint:
                return self.json([self.comments])
            if "/releases?" in endpoint:
                return self.json(self.releases)
            if "/releases/tags/" in endpoint:
                tag = endpoint.split("/releases/tags/", 1)[1]
                release = next((item for item in self.releases if item["tag_name"] == tag), None)
                if release:
                    return self.json(release)
                return CommandResult(
                    1,
                    'HTTP/2.0 404 Not Found\nContent-Type: application/json\n\n{"message":"Not Found"}',
                )
            if "/events?" in endpoint:
                parameters = parse_qs(urlsplit(endpoint).query)
                page = int(parameters.get("page", ["1"])[0])
                size = int(parameters["per_page"][0])
                return self.json(self.events[(page - 1) * size : page * size])
            if "/compare/" in endpoint:
                return self.json({"status": self.ancestor_status})
            if "/issues/" in endpoint:
                number = int(endpoint.rsplit("/", 1)[1])
                if "--method" in argv:
                    desired = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
                    self.issues[number].update(desired)
                    if self.issue_disconnect:
                        raise ConnectionError("Issue updated before connection ended")
                return self.json(self.issues[number])
        if argv[:3] == ["gh", "pr", "list"]:
            branch, base = argv[argv.index("--head") + 1], argv[argv.index("--base") + 1]
            return self.json(
                [pr for pr in self.prs if pr["headRefName"] == branch and pr["baseRefName"] == base]
            )
        if argv[:3] == ["gh", "pr", "view"]:
            return self.json(next(pr for pr in self.prs if pr["number"] == int(argv[3])))
        if argv[:3] == ["gh", "pr", "create"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            self.created_bodies.append(body)
            branch, base = argv[argv.index("--head") + 1], argv[argv.index("--base") + 1]
            self.make_pr(branch, base, self.refs[f"refs/heads/{branch}"])
            if self.create_disconnect:
                raise ConnectionError("PR created but response lost")
            return CommandResult(0, self.prs[-1]["url"])
        if argv[:3] == ["gh", "pr", "merge"]:
            pr = next(pr for pr in self.prs if pr["number"] == int(argv[3]))
            assert argv[argv.index("--match-head-commit") + 1] == pr["headRefOid"]
            pr.update(state="MERGED", mergedAt="2026-10-14T00:00:00Z", mergeCommit={"oid": OTHER})
            if self.merge_disconnect:
                raise ConnectionError("merge completed but response lost")
            return CommandResult(0)
        if argv[:3] == ["gh", "issue", "comment"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            self.comments.append(
                {"id": 1, "body": body, "html_url": "https://forge.example/comment/1"}
            )
            if self.comment_disconnect:
                raise ConnectionError("comment stored but response lost")
            return CommandResult(0, "https://forge.example/comment/1")
        if argv[:3] == ["gh", "run", "list"]:
            return self.json(self.runs)
        if argv[:3] == ["gh", "release", "create"]:
            self.releases.append({"tag_name": argv[3], "draft": False})
            return CommandResult(0)
        if argv[0] == "fixture-check":
            if self.verifier_commit:
                self.workspaces[cwd]["head"] = self.verifier_commit
            return CommandResult(self.verifier_exit, "verified")
        raise AssertionError(f"Unexpected fixture command: {argv}")


@pytest.fixture
def environment(tmp_path: Path) -> tuple[GitHubExecutor, FakeGitHub]:
    remote = FakeGitHub()
    return GitHubExecutor(
        tmp_path / "daemon", runner=remote, lock_directory=tmp_path / "locks"
    ), remote


async def prepare(executor: GitHubExecutor, **extra: Any) -> dict[str, Any]:
    arguments = {
        "attempt_id": "attempt-1",
        "work_item_id": "work-1",
        "branch": "chiliarch/work/a1",
        "base_sha": BASE,
        **extra,
    }
    receipt = await executor.execute(command("prepare_workspace", "prepare", **arguments))
    assert receipt["status"] == "completed", receipt
    return receipt["result"]


async def test_duplicate_command_survives_restart_and_rejects_changed_arguments(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    request = command("inspect_repository")
    first = await executor.execute(request)
    assert first["status"] == "completed"
    assert first["result"]["account"] == "fixture"
    assert first["result"]["push_authentication"] == "unverified"
    count = len(remote.calls)
    restarted = GitHubExecutor(executor.root, runner=remote)
    assert await restarted.execute(request) == first
    changed = await restarted.execute(command("list_events"))
    assert changed["status"] == "failed"
    assert len(remote.calls) == count


async def test_orphaned_intent_does_not_reexecute(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    request = command("inspect_repository")
    await executor.execute(request)
    with executor._database() as database:
        database.execute("UPDATE commands SET state='running',receipt=NULL")
    remote.calls.clear()
    result = await GitHubExecutor(executor.root, runner=remote).execute(request)
    assert result["status"] == "unknown"
    assert remote.calls == []


async def test_cancelled_mutation_is_unknown_and_not_repeated(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    started = asyncio.Event()

    async def interrupted(argv: list[str], cwd: Path) -> CommandResult:
        if argv[:3] == ["gh", "issue", "comment"]:
            started.set()
            await asyncio.Event().wait()
        return await remote(argv, cwd)

    executor.runner = interrupted
    request = command("comment", issue_number=1, body="Review ready")
    task = asyncio.create_task(executor.execute(request))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    repeated = await executor.execute(request)
    assert repeated["status"] == "unknown"
    assert remote.comments == []


async def test_workspace_has_exact_baseline_and_separate_directory(
    environment: tuple[GitHubExecutor, FakeGitHub], tmp_path: Path
) -> None:
    executor, remote = environment
    existing = tmp_path / "personal"
    existing.mkdir()
    sentinel = existing / "unfinished.txt"
    sentinel.write_text("keep me")
    workspace = await prepare(executor, repo_path=str(existing), dependency_shas=[BASE])
    assert Path(workspace["workdir"]) != existing
    assert workspace["base_sha"] == BASE
    assert remote.workspaces[Path(workspace["workdir"])] == {
        "head": BASE,
        "branch": "chiliarch/work/a1",
    }
    assert sentinel.read_text() == "keep me"
    assert not any(argv[:3] == ["gh", "repo", "clone"] for argv, _ in remote.calls)
    assert any(
        argv == ["git", "merge-base", "--is-ancestor", BASE, BASE] for argv, _ in remote.calls
    )


async def test_dependency_not_in_base_prevents_worktree_creation(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.dependencies_valid = False
    result = await executor.execute(
        command(
            "prepare_workspace",
            attempt_id="attempt",
            branch="work",
            base_sha=BASE,
            dependency_shas=[OTHER],
        )
    )
    assert result["status"] != "completed"
    assert remote.workspaces == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": {**REPOSITORY, "repository_id": 999}},
        {"repository": {**REPOSITORY, "host": "-injected"}},
    ],
)
async def test_invalid_repository_identity_cannot_mutate(
    environment: tuple[GitHubExecutor, FakeGitHub], changes: dict[str, Any]
) -> None:
    executor, remote = environment
    request = command("comment", issue_number=1, body="message")
    request["arguments"].update(changes)
    result = await executor.execute(request)
    assert result["status"] == "failed"
    assert remote.comments == []


async def test_integration_branch_is_created_from_exact_sha(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    await prepare(
        executor, integration_branch="chiliarch/goal/integration", integration_base_sha=BASE
    )
    assert remote.refs["refs/heads/chiliarch/goal/integration"] == BASE
    assert all("--force" not in argv for argv, _ in remote.calls)


async def test_deliver_commits_selected_file_and_creates_one_pr(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    workspace = await prepare(executor)
    file = Path(workspace["workdir"]) / "feature.py"
    file.write_text("print('ok')")
    request = command(
        "deliver",
        workspace_id=workspace["workspace_id"],
        files=["feature.py"],
        message="Implement feature",
        pr={"title": "Feature", "body": "literal $(never execute)\nsecond line", "base": "main"},
    )
    first = await executor.execute(request)
    assert first["status"] == "completed", first
    assert first["result"]["head_sha"] == HEAD
    assert first["result"]["pr"]["number"] == 1
    assert remote.refs["refs/heads/chiliarch/work/a1"] == HEAD
    assert "literal $(never execute)\nsecond line" in remote.created_bodies[0]
    assert await executor.execute(request) == first
    assert len(remote.prs) == 1


async def test_unrelated_staged_files_are_preserved(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    workspace = await prepare(executor)
    remote.staged = ["other.py"]
    result = await executor.execute(
        command(
            "deliver",
            workspace_id=workspace["workspace_id"],
            files=["feature.py"],
            message="Feature",
        )
    )
    assert result["status"] == "failed"
    assert remote.staged == ["other.py"]
    assert remote.refs == {}


@pytest.mark.parametrize("file", ["../escape", ":(top)*", ".git/config", "."])
async def test_staging_paths_cannot_escape_workspace(
    environment: tuple[GitHubExecutor, FakeGitHub], file: str
) -> None:
    executor, remote = environment
    workspace = await prepare(executor)
    result = await executor.execute(
        command("deliver", workspace_id=workspace["workspace_id"], files=[file], message="Feature")
    )
    assert result["status"] == "failed"
    assert remote.staged == []
    assert remote.refs == {}


async def test_required_signature_failure_never_pushes(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    workspace = await prepare(executor)
    remote.staged = ["feature.py"]
    remote.signature_valid = False
    result = await executor.execute(
        command(
            "deliver",
            workspace_id=workspace["workspace_id"],
            message="Feature",
            policy={"require_signed_commits": True},
        )
    )
    assert result["status"] != "completed"
    assert remote.refs == {}
    commits = [argv for argv, _ in remote.calls if argv[:2] == ["git", "commit"]]
    assert len(commits) == 1 and "-S" in commits[0]
    assert "private signing details" not in result["error"]


async def test_push_response_loss_is_reconciled_without_second_push(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    workspace = await prepare(executor)
    remote.push_disconnect = True
    request = command("deliver", workspace_id=workspace["workspace_id"])
    first = await executor.execute(request)
    assert first["status"] == "unknown"
    assert remote.refs["refs/heads/chiliarch/work/a1"] == BASE
    result = await executor.execute(
        command("reconcile", "recovery", original_command_id="operation")
    )
    assert result["result"]["original_status"] == "completed"
    assert len([argv for argv, _ in remote.calls if argv[:2] == ["git", "push"]]) == 1
    assert (await executor.execute(request))["status"] == "completed"


async def test_pr_response_loss_is_reconciled_without_duplicate(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.refs["refs/heads/integration"] = HEAD
    remote.create_disconnect = True
    request = command(
        "create_pull_request",
        head="integration",
        base="main",
        expected_head_sha=HEAD,
        title="Complete goal",
        body="Summary",
    )
    assert (await executor.execute(request))["status"] == "unknown"
    result = await executor.execute(
        command("reconcile", "recovery", original_command_id="operation")
    )
    assert result["result"]["original_status"] == "completed"
    assert result["result"]["receipt"]["result"]["pr"]["head_sha"] == HEAD
    assert len(remote.prs) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"headRefOid": OTHER},
        {"baseRefOid": OTHER},
        {"statusCheckRollup": []},
        {
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                }
            ]
        },
        {
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ]
        },
    ],
)
async def test_merge_revalidates_revision_and_required_checks(
    environment: tuple[GitHubExecutor, FakeGitHub], changes: dict[str, Any]
) -> None:
    executor, remote = environment
    remote.make_pr().update(changes)
    result = await executor.execute(
        command(
            "merge_pull_request",
            pr_number=1,
            head_sha=HEAD,
            base_sha=BASE,
            required_checks=["tests"],
        )
    )
    assert result["status"] == "failed"
    assert remote.prs[0]["mergedAt"] is None
    assert not any(argv[:3] == ["gh", "pr", "merge"] for argv, _ in remote.calls)


async def test_merge_lost_response_reconciles_exact_commit(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.make_pr()
    remote.merge_disconnect = True
    assert (
        await executor.execute(
            command(
                "merge_pull_request",
                pr_number=1,
                head_sha=HEAD,
                base_sha=BASE,
                required_checks=["tests"],
            )
        )
    )["status"] == "unknown"
    result = await executor.execute(
        command("reconcile", "recovery", original_command_id="operation")
    )
    assert result["result"]["receipt"]["result"]["pr"]["merge_commit_sha"] == OTHER
    merges = [argv for argv, _ in remote.calls if argv[:3] == ["gh", "pr", "merge"]]
    assert len(merges) == 1
    assert "--admin" not in merges[0]


async def test_comment_lost_response_uses_marker_without_second_comment(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.comment_disconnect = True
    assert (await executor.execute(command("comment", issue_number=3, body="Reviewer A: ready")))[
        "status"
    ] == "unknown"
    result = await executor.execute(
        command("reconcile", "recovery", original_command_id="operation")
    )
    assert result["result"]["original_status"] == "completed"
    assert len(remote.comments) == 1


async def test_tag_conflict_is_never_overwritten(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.refs["refs/tags/v1.0.0"] = OTHER
    result = await executor.execute(command("publish_tag", tag="v1.0.0", commit_sha=HEAD))
    assert result["status"] == "failed"
    assert remote.refs["refs/tags/v1.0.0"] == OTHER
    assert not any(argv[:2] == ["git", "push"] for argv, _ in remote.calls)


async def test_workflow_owns_release_creation_and_observation_tracks_exact_tag(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.runs = [
        {"headSha": HEAD, "headBranch": "main", "status": "completed", "conclusion": "success"},
        {"headSha": HEAD, "headBranch": "v1.0.0", "status": "in_progress", "conclusion": ""},
    ]
    result = await executor.execute(
        command("publish_tag", tag="v1.0.0", commit_sha=HEAD, release_owner="workflow")
    )
    assert result["status"] == "completed", result
    assert result["result"]["published"] is False
    assert len(result["result"]["workflow_runs"]) == 1
    assert result["result"]["workflow_runs"][0]["headBranch"] == "v1.0.0"
    assert remote.releases == []
    assert remote.refs["refs/tags/v1.0.0"] == HEAD


async def test_daemon_release_owner_creates_release_once(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    result = await executor.execute(
        command(
            "publish_tag",
            tag="v1.0.0",
            commit_sha=HEAD,
            release_owner="daemon",
            release_notes="User-facing changes",
        )
    )
    assert result["status"] == "completed", result
    assert result["result"]["published"] is True
    again = await executor.execute(
        command("publish_tag", "again", tag="v1.0.0", commit_sha=HEAD, release_owner="daemon")
    )
    assert again["status"] == "completed"
    assert len(remote.releases) == 1


async def test_cleanup_requires_clean_and_published_head(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    workspace = await prepare(executor)
    args = {"workspace_id": workspace["workspace_id"]}
    remote.dirty = "?? keep.txt"
    assert (await executor.execute(command("cleanup_workspace", "dirty", **args)))[
        "status"
    ] == "failed"
    remote.dirty = ""
    assert (await executor.execute(command("cleanup_workspace", "unpublished", **args)))[
        "status"
    ] == "failed"
    remote.refs["refs/heads/chiliarch/work/a1"] = BASE
    result = await executor.execute(command("cleanup_workspace", "clean", **args))
    assert result["result"]["removed"] is True
    assert remote.refs["refs/heads/chiliarch/work/a1"] == BASE
    assert not Path(workspace["workdir"]).exists()


async def test_unverified_native_execution_blocks_delivery(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    from chiliarch.adapters.runtime import workspace_owner

    executor, remote = environment
    workspace = await prepare(executor)
    async with workspace_owner(
        Path(workspace["workdir"]), lock_directory=executor.lock_directory
    ) as lease:
        await lease.begin_execution("native-task")
    result = await executor.execute(command("deliver", workspace_id=workspace["workspace_id"]))
    assert result["status"] == "failed"
    assert remote.refs == {}


async def test_project_command_is_scoped_to_registered_workspace(
    environment: tuple[GitHubExecutor, FakeGitHub], tmp_path: Path
) -> None:
    executor, remote = environment
    bad = await executor.execute(
        command("run_project_command", "bad", workdir=str(tmp_path), argv=["fixture-check"])
    )
    assert bad["status"] == "failed"
    workspace = await prepare(executor)
    result = await executor.execute(
        command(
            "run_project_command",
            "valid",
            workdir=workspace["workdir"],
            argv=["fixture-check", "argument with spaces"],
        )
    )
    assert result["result"]["passed"] is True
    assert result["result"]["head_sha"] == BASE
    assert (["fixture-check", "argument with spaces"], Path(workspace["workdir"])) in remote.calls


async def test_events_return_cursor_and_expose_retention_gap(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.events = [{"id": "3"}, {"id": "2"}, {"id": "1"}]
    result = await executor.execute(command("list_events", cursor="1"))
    assert result["result"] == {
        "events": [{"id": "2"}, {"id": "3"}],
        "cursor": "3",
        "cursor_found": True,
    }
    gap = await executor.execute(command("list_events", "gap", cursor="old"))
    assert gap["result"]["cursor_found"] is False


@pytest.mark.parametrize("cursor,pages", [("199", 2), ("100", 3)])
async def test_events_follow_pages_to_the_previous_cursor(
    environment: tuple[GitHubExecutor, FakeGitHub], cursor: str, pages: int
) -> None:
    executor, remote = environment
    remote.events = [{"id": str(value)} for value in range(350, 0, -1)]
    result = await executor.execute(command("list_events", cursor=cursor))
    assert result["status"] == "completed"
    assert result["result"] == {
        "events": [{"id": str(value)} for value in range(int(cursor) + 1, 351)],
        "cursor": "350",
        "cursor_found": True,
    }
    queries = [
        argv[4] for argv, _ in remote.calls if argv[:2] == ["gh", "api"] and "/events?" in argv[4]
    ]
    assert queries == [
        f"repos/team/project/events?per_page=100&page={page}" for page in range(1, pages + 1)
    ]


@pytest.mark.parametrize("count,pages", [(150, 2), (350, 3)])
async def test_events_missing_cursor_stops_at_available_history_or_api_limit(
    environment: tuple[GitHubExecutor, FakeGitHub], count: int, pages: int
) -> None:
    executor, remote = environment
    remote.events = [{"id": str(value)} for value in range(count, 0, -1)]
    result = await executor.execute(command("list_events", cursor="unavailable"))
    assert result["status"] == "completed"
    assert result["result"]["cursor_found"] is False
    assert result["result"]["cursor"] == str(count)
    assert len(result["result"]["events"]) == min(count, 300)
    assert (
        len(
            [
                argv
                for argv, _ in remote.calls
                if argv[:2] == ["gh", "api"] and "/events?" in argv[4]
            ]
        )
        == pages
    )


async def test_first_event_observation_does_not_import_all_historic_pages(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.events = [{"id": str(value)} for value in range(250, 0, -1)]
    result = await executor.execute(command("list_events"))
    assert result["result"] == {
        "events": [{"id": str(value)} for value in range(151, 251)],
        "cursor": "250",
        "cursor_found": True,
    }
    assert (
        len(
            [
                argv
                for argv, _ in remote.calls
                if argv[:2] == ["gh", "api"] and "/events?" in argv[4]
            ]
        )
        == 1
    )


async def test_event_pagination_deduplicates_page_shift_and_keeps_initial_cursor(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.events = [{"id": str(value)} for value in range(250, 0, -1)]

    async def shifting(argv: list[str], cwd: Path) -> CommandResult:
        if argv[:2] == ["gh", "api"] and argv[4].endswith("/events?per_page=100&page=2"):
            remote.events.insert(0, {"id": "251"})
        return await remote(argv, cwd)

    executor.runner = shifting
    result = await executor.execute(command("list_events", cursor="100"))
    assert result["result"] == {
        "events": [{"id": str(value)} for value in range(101, 251)],
        "cursor": "250",
        "cursor_found": True,
    }


async def test_final_integration_pr_rejects_missing_dependency(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.refs["refs/heads/integration"] = HEAD
    remote.ancestor_status = "diverged"
    result = await executor.execute(
        command(
            "create_pull_request",
            head="integration",
            base="main",
            expected_head_sha=HEAD,
            title="Complete goal",
            dependency_shas=[OTHER],
        )
    )
    assert result["status"] == "failed"
    assert remote.prs == []


async def test_existing_lightweight_tag_cannot_satisfy_signed_policy(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.refs["refs/tags/v1.0.0"] = HEAD
    result = await executor.execute(
        command("publish_tag", tag="v1.0.0", commit_sha=HEAD, policy={"require_signed_tags": True})
    )
    assert result["status"] == "failed"
    assert not any(argv[:2] == ["git", "push"] for argv, _ in remote.calls)


@pytest.fixture
def real_git(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A local-only Git universe, isolated from the developer's signing config."""
    configuration = tmp_path / "gitconfig"
    configuration.write_text("[user]\n name = Fixture\n email = fixture@example.invalid\n")
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(configuration),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    source, bare = tmp_path / "source", tmp_path / "remote.git"
    source.mkdir()

    def git(directory: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=directory, env=environment, capture_output=True, check=True
        )

    git(tmp_path, "init", "--bare", str(bare))
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("Original baseline\n")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "Baseline")
    git(source, "remote", "add", "origin", str(bare))
    git(source, "push", "origin", "main")
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return source, bare, environment


async def test_real_git_worktree_commit_delivery_preserves_personal_checkout(
    real_git: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    source, bare, environment = real_git
    github = FakeGitHub()

    def local_git(argv: list[str], directory: Path) -> CommandResult:
        process = subprocess.run(
            argv, cwd=directory, env=environment, capture_output=True, text=True
        )
        return CommandResult(process.returncode, process.stdout, process.stderr)

    base = local_git(["git", "rev-parse", "HEAD"], source).stdout.strip()
    # Existing checkout remains dirty and on main throughout the managed work.
    (source / "unfinished.txt").write_text("User draft")
    local_git(["git", "remote", "set-url", "origin", github.remote_url], source)

    async def runner(argv: list[str], cwd: Path) -> CommandResult:
        if argv[0] == "git":
            translated = argv[:]
            if argv[1:2] in (["fetch"], ["push"]):
                translated[translated.index("origin")] = str(bare)
            return await asyncio.to_thread(local_git, translated, cwd)
        return await github(argv, cwd)

    executor = GitHubExecutor(
        tmp_path / "executor", runner=runner, lock_directory=tmp_path / "locks"
    )
    prepared = await executor.execute(
        command(
            "prepare_workspace",
            "prepare-real",
            attempt_id="attempt",
            branch="chiliarch/work/a1",
            base_sha=base,
            repo_path=str(source),
            dependency_shas=[base],
        )
    )
    assert prepared["status"] == "completed", prepared
    workspace = prepared["result"]
    workdir = Path(workspace["workdir"])
    (workdir / "feature with spaces.py").write_text("print('feature')\n")
    (workdir / "leave.txt").write_text("Not part of this delivery\n")
    delivered = await executor.execute(
        command(
            "deliver",
            "deliver-real",
            workspace_id=workspace["workspace_id"],
            files=["feature with spaces.py"],
            message="Add feature",
        )
    )
    assert delivered["status"] == "completed", delivered
    head = delivered["result"]["head_sha"]
    assert head != base
    remote_head = local_git(
        ["git", "rev-parse", "refs/heads/chiliarch/work/a1"], bare
    ).stdout.strip()
    assert head == remote_head
    committed = local_git(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", head], workdir
    ).stdout.splitlines()
    assert committed == ["feature with spaces.py"]
    assert (workdir / "leave.txt").exists()
    assert local_git(["git", "branch", "--show-current"], source).stdout.strip() == "main"
    assert local_git(["git", "rev-parse", "HEAD"], source).stdout.strip() == base
    assert (source / "unfinished.txt").read_text() == "User draft"


@pytest.mark.parametrize("failed, changed", [(1, None), (0, OTHER)])
async def test_release_requires_successful_verification_of_exact_commit(
    environment: tuple[GitHubExecutor, FakeGitHub], failed: int, changed: str | None
) -> None:
    executor, remote = environment
    remote.verifier_exit, remote.verifier_commit = failed, changed
    result = await executor.execute(command("publish_tag", tag="v1.0.0", commit_sha=HEAD))
    assert result["status"] != "completed"
    assert "refs/tags/v1.0.0" not in remote.refs
    assert remote.releases == []


async def test_release_verification_records_exact_version_and_commit(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    result = await executor.execute(
        command(
            "publish_tag",
            tag="v1.0.0",
            commit_sha=HEAD,
            verify_argv=["fixture-check", "{version}", "{commit_sha}"],
        )
    )
    assert result["status"] == "completed", result
    assert result["result"]["verification"] == {
        "commit_sha": HEAD,
        "version": "1.0.0",
        "command": ["fixture-check", "1.0.0", HEAD],
        "exit_code": 0,
    }
    assert remote.refs["refs/tags/v1.0.0"] == HEAD


def issue() -> dict[str, Any]:
    return {
        "number": 5,
        "html_url": "https://forge.example/team/project/issues/5",
        "title": "Fix installation",
        "body": "Full original issue\nSensitive-looking data remains verbatim",
        "state": "open",
        "updated_at": "2026-10-14T00:00:00Z",
        "labels": [{"name": "bug"}],
    }


async def test_explicit_issue_import_preserves_original_and_rejects_pr(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.issues[5] = issue()
    observed = await executor.execute(command("inspect_issue", issue_number=5))
    assert observed["status"] == "completed"
    assert observed["result"]["issue"]["body"] == remote.issues[5]["body"]
    assert observed["result"]["issue"]["labels"] == ["bug"]
    remote.issues[5]["pull_request"] = {}
    rejected = await executor.execute(command("inspect_issue", "pr-not-issue", issue_number=5))
    assert rejected["status"] == "failed"


async def test_issue_update_lost_response_queries_without_second_mutation(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.issues[5] = issue()
    remote.issue_disconnect = True
    request = command("update_issue", issue_number=5, state="closed", labels=["done"])
    result = await executor.execute(request)
    assert result["status"] == "unknown"
    assert remote.issues[5]["state"] == "closed"
    assert remote.issues[5]["labels"] == ["done"]
    recovered = await executor.execute(
        command("reconcile", "recover", original_command_id="operation")
    )
    assert recovered["result"]["original_status"] == "completed"
    assert recovered["result"]["original_result"]["issue"]["state"] == "closed"
    assert len([argv for argv, _ in remote.calls if "--method" in argv]) == 1
    assert (await executor.execute(request))["status"] == "completed"


async def test_issue_update_respects_expected_observation_and_explicit_empty_labels(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment
    remote.issues[5] = issue()
    rejected = await executor.execute(
        command("update_issue", "stale", issue_number=5, expected_updated_at="old", state="closed")
    )
    assert rejected["status"] == "failed"
    assert remote.issues[5]["state"] == "open"
    accepted = await executor.execute(command("update_issue", "clear", issue_number=5, labels=[]))
    assert accepted["status"] == "completed"
    assert remote.issues[5]["labels"] == []
    noop = await executor.execute(command("update_issue", "noop", issue_number=5, labels=[]))
    assert noop["result"]["changed"] is False
    assert len([argv for argv, _ in remote.calls if "--method" in argv]) == 1


async def test_api_rate_limit_uses_observed_retry_after_without_exposing_body(
    tmp_path: Path,
) -> None:
    async def limited(argv: list[str], cwd: Path) -> CommandResult:
        return CommandResult(
            1,
            'HTTP/2.0 429 Too Many Requests\r\nRetry-After: 63\r\n\r\n{"message":"private diagnostics"}',
            "private credentials",
        )

    executor = GitHubExecutor(tmp_path, runner=limited)
    result = await executor.execute(command("list_events"))
    assert result["status"] == "failed"
    assert result["result"]["retry_after"] == 63
    assert "private" not in result["error"]


async def test_successful_api_headers_are_removed_before_json_decode(
    environment: tuple[GitHubExecutor, FakeGitHub],
) -> None:
    executor, remote = environment

    async def headers(argv: list[str], cwd: Path) -> CommandResult:
        result = await remote(argv, cwd)
        if argv[:2] == ["gh", "api"]:
            return CommandResult(
                0, "HTTP/2.0 200 OK\nContent-Type: application/json\n\n" + result.stdout
            )
        return result

    executor.runner = headers
    result = await executor.execute(command("inspect_repository"))
    assert result["status"] == "completed"
    assert result["result"]["repository_id"] == 17
