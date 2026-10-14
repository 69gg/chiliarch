"""Deterministic, outbound Git/GitHub operations owned by one local daemon.

The journal records intent before a subprocess can produce an external effect.
A missing result is uncertainty, never an invitation to execute the command again.
Credentials and signing settings are inherited from the local git/gh installation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from filelock import FileLock, Timeout

from chiliarch.adapters.processes import start_process_tree
from chiliarch.adapters.runtime import workspace_owner

logger = logging.getLogger("chiliarch.github")

# GitHub's Events API exposes at most 300 entries, with at most 100 per page.
GITHUB_EVENTS_PAGE_SIZE = 100
GITHUB_EVENTS_MAX_PAGES = 3


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], Path], Awaitable[CommandResult]]


class GitHubCommandError(RuntimeError):
    """A local command failed; raw CLI diagnostics remain on the local machine."""


class GitHubExecutor:
    """Execute an authenticated platform command once, with durable reconciliation.

    ``runner`` is injectable for deterministic tests. Production runners always use
    argv and the existing cross-platform process-tree owner, without a shell.
    """

    def __init__(
        self,
        root: Path,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 120,
        lock_directory: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Command timeout must be positive")
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal = self.root / "commands.sqlite3"
        self.runner = runner or self._run_process
        self.timeout_seconds = timeout_seconds
        self.lock_directory = lock_directory
        self._lock = asyncio.Lock()
        self._command_id = ""
        self._progress: dict[str, Any] = {}
        with self._database() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, receipt TEXT, progress TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.journal)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def _run_process(self, argv: list[str], cwd: Path) -> CommandResult:
        tree = await start_process_tree(argv, cwd, 1024 * 1024)
        try:
            stdout, stderr = await tree.process.communicate()
            return CommandResult(
                tree.process.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        finally:
            # This also joins descendants after cancellation/timeout. A subsequent
            # command must not race a child that survived its CLI's early exit.
            cleanup = asyncio.create_task(tree.terminate(0.1))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    @staticmethod
    def _receipt(
        command_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {"command_id": command_id, "status": status, "result": result or {}, "error": error}

    def _save_receipt(self, receipt: dict[str, Any]) -> None:
        with self._database() as database:
            database.execute(
                "UPDATE commands SET state = ?, receipt = ? WHERE id = ?",
                (receipt["status"], json.dumps(receipt), receipt["command_id"]),
            )

    def _checkpoint(self, **values: Any) -> None:
        self._progress.update(values)
        with self._database() as database:
            database.execute(
                "UPDATE commands SET progress = ? WHERE id = ?",
                (json.dumps(self._progress), self._command_id),
            )

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
            raise ValueError("A command_id of 1 to 200 characters is required")
        try:
            serialized = json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if not isinstance(command.get("arguments"), dict):
                raise ValueError("Command arguments must be an object")
        except (ValueError, TypeError) as error:
            return self._receipt(command_id, "failed", error=str(error))
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        # One journal owner serializes shared clone metadata across identities.
        # A live owner keeps its lock; a new owner can recognize orphaned intent.
        async with self._lock:
            owner = FileLock(self.root / "executor.lock", timeout=0, thread_local=False)
            try:
                while True:
                    try:
                        owner.acquire()
                        break
                    except Timeout:
                        await asyncio.sleep(0.05)
                with self._database() as database:
                    previous = database.execute(
                        "SELECT * FROM commands WHERE id = ?", (command_id,)
                    ).fetchone()
                    if previous is not None:
                        if previous["fingerprint"] != fingerprint:
                            return self._receipt(
                                command_id,
                                "failed",
                                error="Command ID was reused with different arguments",
                            )
                        if previous["receipt"]:
                            return dict(json.loads(previous["receipt"]))
                        receipt = self._receipt(
                            command_id,
                            "unknown",
                            json.loads(previous["progress"]),
                            "Previous execution ended without a confirmed result; reconcile before continuing",
                        )
                        database.execute(
                            "UPDATE commands SET state = ?, receipt = ? WHERE id = ?",
                            ("unknown", json.dumps(receipt), command_id),
                        )
                        return receipt
                    database.execute(
                        "INSERT INTO commands(id,fingerprint,request,state) VALUES (?,?,?,?)",
                        (command_id, fingerprint, serialized, "running"),
                    )
                self._command_id, self._progress = command_id, {}
                handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
                    "inspect_repository": self._inspect_repository,
                    "inspect_issue": self._inspect_issue,
                    "comment_issue": self._comment,
                    "update_issue": self._update_issue,
                    "prepare_workspace": self._prepare_workspace,
                    "deliver": self._deliver,
                    "create_pull_request": self._create_pull_request,
                    "inspect_pull_request": self._inspect_pull_request,
                    "merge_pull_request": self._merge_pull_request,
                    "comment": self._comment,
                    "publish_tag": self._publish_tag,
                    "inspect_release": self._inspect_release,
                    "list_events": self._list_events,
                    "cleanup_workspace": self._cleanup_workspace,
                    "run_project_command": self._run_project_command,
                    "reconcile": self._reconcile,
                }
                try:
                    action = command.get("action")
                    if not isinstance(action, str) or action not in handlers:
                        raise ValueError("Unsupported repository action")
                    if action != "reconcile":
                        await self._verify_identity(command["arguments"])
                    result = await handlers[action](command["arguments"])
                    if "retry_after" in self._progress:
                        result.setdefault("retry_after", self._progress["retry_after"])
                    receipt = self._receipt(command_id, "completed", result)
                except asyncio.CancelledError:
                    self._save_receipt(
                        self._receipt(
                            command_id, "unknown", self._progress, "Execution was interrupted"
                        )
                    )
                    raise
                except Exception as error:
                    # CLI errors do not establish whether a remote mutation happened.
                    status = "unknown" if self._progress.get("effects_started") else "failed"
                    safe_error = (
                        str(error)
                        if isinstance(error, (ValueError, GitHubCommandError))
                        else f"Repository execution failed ({type(error).__name__})"
                    )
                    receipt = self._receipt(command_id, status, self._progress, safe_error)
                self._save_receipt(receipt)
                logger.info(
                    "github.command_finished",
                    extra={
                        "command_id": command_id,
                        "action": command.get("action"),
                        "status": receipt["status"],
                    },
                )
                return receipt
            finally:
                owner.release()

    async def _call(
        self,
        argv: list[str],
        cwd: Path | None = None,
        *,
        mutates: bool = False,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        if mutates:
            self._checkpoint(effects_started=True)
        async with asyncio.timeout(timeout_seconds or self.timeout_seconds):
            result = await self.runner(argv, cwd or self.root)
        if check and result.returncode:
            raise GitHubCommandError(
                f"{argv[0]} operation failed (exit {result.returncode}); inspect local authentication, permissions and signing settings"
            )
        return result

    async def _git(
        self, cwd: Path, *arguments: str, mutates: bool = False, check: bool = True
    ) -> CommandResult:
        return await self._call(["git", *arguments], cwd, mutates=mutates, check=check)

    @staticmethod
    def _text(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError(f"{key} must be a nonempty string")
        return value

    @classmethod
    def _sha(cls, arguments: dict[str, Any], key: str) -> str:
        value = cls._text(arguments, key)
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
            raise ValueError(f"{key} must be a full commit SHA")
        return value.lower()

    @classmethod
    def _repository(cls, arguments: dict[str, Any]) -> dict[str, Any]:
        repository = arguments.get("repository")
        if not isinstance(repository, dict):
            raise ValueError("repository must identify a GitHub host, owner and name")
        for key in ("host", "owner", "name"):
            value = cls._text(repository, key)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or ".." in value:
                raise ValueError(f"Invalid repository {key}")
        return repository

    @classmethod
    def _repo_slug(cls, arguments: dict[str, Any]) -> str:
        repository = cls._repository(arguments)
        return f"{repository['host']}/{repository['owner']}/{repository['name']}"

    @staticmethod
    def _api_body(stdout: str) -> tuple[dict[str, str], str]:
        lines = stdout.replace("\r\n", "\n")
        if not lines.startswith("HTTP/"):
            return {}, stdout
        header, separator, body = lines.partition("\n\n")
        if not separator:
            raise GitHubCommandError("GitHub returned incomplete response headers")
        headers: dict[str, str] = {}
        status_line = header.splitlines()[0].split()
        if len(status_line) >= 2:
            headers["status"] = status_line[1]
        for line in header.splitlines()[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        return headers, body

    def _observe_retry_after(self, headers: dict[str, str]) -> None:
        value = headers.get("retry-after")
        delay: float | None = None
        if value is not None:
            try:
                delay = float(value)
            except ValueError:
                try:
                    instant = parsedate_to_datetime(value)
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=UTC)
                    delay = (instant - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    pass
        if delay is None and headers.get("x-ratelimit-remaining") == "0":
            try:
                delay = float(headers["x-ratelimit-reset"]) - datetime.now(UTC).timestamp()
            except (KeyError, ValueError):
                pass
        if delay is not None and math.isfinite(delay) and delay > 0:
            self._checkpoint(retry_after=delay)

    async def _api(
        self,
        arguments: dict[str, Any],
        endpoint: str,
        *,
        mutation: dict[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        repository = self._repository(arguments)
        argv = ["gh", "api", "--hostname", repository["host"], endpoint, "--include"]
        if mutation is not None:
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                body_file = Path(temporary) / "request.json"
                body_file.write_text(json.dumps(mutation, ensure_ascii=False), encoding="utf-8")
                result = await self._call(
                    [*argv, "--method", "PATCH", "--input", str(body_file)],
                    mutates=True,
                    check=False,
                )
        else:
            result = await self._call(argv, check=False)
        headers, body = self._api_body(result.stdout)
        self._observe_retry_after(headers)
        if result.returncode:
            if missing_ok and headers.get("status") == "404":
                return None
            raise GitHubCommandError(f"GitHub API operation failed (exit {result.returncode})")
        return json.loads(body)

    async def _inspect_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        number = self._number(arguments, "issue_number")
        value = await self._api(arguments, self._endpoint(arguments, f"/issues/{number}"))
        if not isinstance(value, dict) or "pull_request" in value:
            raise ValueError("The requested GitHub item is not an issue")
        return {
            "issue": {
                "number": value["number"],
                "url": value["html_url"],
                "title": value["title"],
                "body": value.get("body") or "",
                "state": value["state"],
                "updated_at": value["updated_at"],
                "labels": [
                    label["name"] if isinstance(label, dict) else label
                    for label in value.get("labels", [])
                ],
            }
        }

    async def _update_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        desired: dict[str, Any] = {}
        if "state" in arguments:
            if arguments["state"] not in {"open", "closed"}:
                raise ValueError("Issue state must be open or closed")
            desired["state"] = arguments["state"]
        if "labels" in arguments:
            labels = arguments["labels"]
            if not isinstance(labels, list) or not all(
                isinstance(value, str) and value.strip() and "\0" not in value for value in labels
            ):
                raise ValueError("Issue labels must be a list of nonempty names")
            desired["labels"] = list(dict.fromkeys(labels))
        if not desired:
            raise ValueError("An issue state or explicit label set is required")
        observed = (await self._inspect_issue(arguments))["issue"]
        if (
            arguments.get("expected_updated_at")
            and arguments["expected_updated_at"] != observed["updated_at"]
        ):
            raise ValueError("Issue changed since the update was requested")
        if self._issue_matches(observed, desired):
            return {"issue": observed, "changed": False}
        self._checkpoint(issue_number=observed["number"], desired_issue=desired)
        await self._api(
            arguments, self._endpoint(arguments, f"/issues/{observed['number']}"), mutation=desired
        )
        after = (await self._inspect_issue(arguments))["issue"]
        if not self._issue_matches(after, desired):
            raise GitHubCommandError("Issue update could not be confirmed")
        return {"issue": after, "changed": True}

    @staticmethod
    def _issue_matches(issue: dict[str, Any], desired: dict[str, Any]) -> bool:
        return all(
            set(issue.get(key, [])) == set(value) if key == "labels" else issue.get(key) == value
            for key, value in desired.items()
        )

    @classmethod
    def _endpoint(cls, arguments: dict[str, Any], suffix: str = "") -> str:
        repository = cls._repository(arguments)
        return f"repos/{repository['owner']}/{repository['name']}" + suffix

    async def _verify_identity(self, arguments: dict[str, Any]) -> dict[str, Any]:
        repository = self._repository(arguments)
        observed = await self._api(arguments, self._endpoint(arguments))
        if not isinstance(observed, dict):
            raise ValueError("GitHub did not return a repository object")
        expected = repository.get("repository_id")
        if expected is not None and str(observed.get("id")) != str(expected):
            raise ValueError("Repository ID changed; update the project binding explicitly")
        return observed

    async def _validate_ref(self, value: str, *, tag: bool = False) -> None:
        if not value or value.startswith("-") or "\0" in value:
            raise ValueError("Invalid ref name")
        arguments = (
            ["check-ref-format", f"refs/tags/{value}"]
            if tag
            else ["check-ref-format", "--branch", value]
        )
        result = await self._git(self.root, *arguments, check=False)
        if result.returncode:
            raise ValueError("Invalid Git branch or tag name")

    @staticmethod
    def _same_remote(url: str, repository: dict[str, Any]) -> bool:
        host: str | None
        if url.startswith("git@") and ":" in url:
            authority, path = url[4:].split(":", 1)
            host = authority
        else:
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "ssh"} or parsed.password:
                return False
            if parsed.scheme == "https" and parsed.username:
                return False
            host, path = parsed.hostname, parsed.path.lstrip("/")
        path = path.removesuffix(".git").rstrip("/")
        return (
            str(host).casefold() == repository["host"].casefold()
            and path.casefold() == f"{repository['owner']}/{repository['name']}".casefold()
        )

    def _project_directory(self, arguments: dict[str, Any]) -> Path:
        project_id = self._text(arguments, "project_id")
        identity = hashlib.sha256(project_id.encode()).hexdigest()[:24]
        return self.root / "repositories" / identity

    async def _clone(self, arguments: dict[str, Any], sha: str) -> Path:
        repository = self._repository(arguments)
        explicit = arguments.get("repo_path")
        clone = (
            await asyncio.to_thread(lambda: Path(explicit).expanduser().resolve())
            if explicit
            else self._project_directory(arguments)
        )
        if explicit and not clone.is_dir():
            raise ValueError("Bound repository directory does not exist")
        if not clone.exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            await self._call(
                [
                    "gh",
                    "repo",
                    "clone",
                    self._repo_slug(arguments),
                    str(clone),
                    "--",
                    "--no-checkout",
                ],
                mutates=True,
            )
        remote = (await self._git(clone, "remote", "get-url", "origin")).stdout.strip()
        if not self._same_remote(remote, repository):
            raise ValueError("The local origin does not match the registered GitHub repository")
        await self._git(clone, "fetch", "--no-tags", "origin", sha, mutates=True)
        observed = (
            await self._git(clone, "rev-parse", "--verify", f"{sha}^{{commit}}")
        ).stdout.strip()
        if observed != sha:
            raise ValueError("Requested base commit could not be verified")
        return clone

    async def _inspect_repository(self, arguments: dict[str, Any]) -> dict[str, Any]:
        git = await self._call(["git", "--version"])
        gh = await self._call(["gh", "--version"])
        account = await self._api(arguments, "user")
        repository = await self._verify_identity(arguments)
        clone = self._project_directory(arguments)
        explicit = arguments.get("repo_path")
        if explicit:
            clone = await asyncio.to_thread(lambda: Path(explicit).expanduser().resolve())
        settings: dict[str, str | bool] = {}
        for key in ("user.name", "user.email", "commit.gpgsign", "tag.gpgsign", "gpg.format"):
            value = await self._git(
                clone if clone.is_dir() else self.root, "config", "--get", key, check=False
            )
            settings[key] = value.stdout.strip() if value.returncode == 0 else ""
        # A key identifier is not proof that the key can be unlocked and used.
        signing = await self._git(
            clone if clone.is_dir() else self.root,
            "config",
            "--get",
            "user.signingkey",
            check=False,
        )
        settings["signing_key_configured"] = signing.returncode == 0 and bool(
            signing.stdout.strip()
        )
        return {
            "repository_id": repository.get("id"),
            "default_branch": repository.get("default_branch"),
            "permissions": repository.get("permissions", {}),
            "account": account.get("login"),
            "git_version": git.stdout.strip(),
            "gh_version": gh.stdout.splitlines()[0] if gh.stdout else "",
            "git_settings": settings,
            "push_authentication": "unverified",
            "signing_availability": "unverified",
        }

    async def _remote_ref(self, arguments: dict[str, Any], ref: str) -> dict[str, Any] | None:
        prefix = ref.removeprefix("refs/")
        values = await self._api(
            arguments, self._endpoint(arguments, "/git/matching-refs/" + quote(prefix, safe="/"))
        )
        if not isinstance(values, list):
            raise ValueError("GitHub ref observation was not a list")
        return next((item for item in values if item.get("ref") == ref), None)

    async def _prepare_workspace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sha = self._sha(arguments, "base_sha")
        branch = self._text(arguments, "branch")
        await self._validate_ref(branch)
        attempt = self._text(arguments, "attempt_id")
        workspace_id = hashlib.sha256(
            f"{self._text(arguments, 'project_id')}:{attempt}".encode()
        ).hexdigest()
        with self._database() as database:
            existing = database.execute(
                "SELECT metadata FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if existing:
            metadata = dict(json.loads(existing["metadata"]))
            if (
                metadata["base_sha"] != sha
                or metadata["branch"] != branch
                or metadata["repository"] != arguments["repository"]
            ):
                raise ValueError("Attempt already owns a different workspace")
            await self._verify_workspace(metadata, arguments)
            return metadata
        clone = await self._clone(arguments, sha)
        dependencies = arguments.get("dependency_shas", [])
        if not isinstance(dependencies, list):
            raise ValueError("dependency_shas must be a list")
        for dependency in dependencies:
            dependency = self._sha({"dependency_sha": dependency}, "dependency_sha")
            if (
                await self._git(clone, "merge-base", "--is-ancestor", dependency, sha, check=False)
            ).returncode:
                raise ValueError("The requested base does not include every dependency commit")
        integration = arguments.get("integration_branch")
        if integration:
            await self._validate_ref(integration)
            integration_base = self._sha(arguments, "integration_base_sha")
            remote = await self._remote_ref(arguments, f"refs/heads/{integration}")
            if remote is None:
                await self._git(
                    clone, "fetch", "--no-tags", "origin", integration_base, mutates=True
                )
                await self._git(
                    clone,
                    "push",
                    "origin",
                    f"{integration_base}:refs/heads/{integration}",
                    mutates=True,
                )
            else:
                integration_sha = remote["object"]["sha"]
                await self._git(
                    clone, "fetch", "--no-tags", "origin", integration_sha, mutates=True
                )
                if (
                    await self._git(
                        clone,
                        "merge-base",
                        "--is-ancestor",
                        integration_base,
                        integration_sha,
                        check=False,
                    )
                ).returncode:
                    raise ValueError("Existing integration branch has an unrelated baseline")
        if await self._remote_ref(arguments, f"refs/heads/{branch}") is not None:
            raise ValueError("Attempt branch already exists remotely; reconcile ownership first")
        path = self.root / "worktrees" / workspace_id
        if path.exists():
            raise ValueError("Unregistered worktree already exists; reconcile before continuing")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint(
            workspace_id=workspace_id,
            workdir=str(path),
            branch=branch,
            base_sha=sha,
            clone=str(clone),
        )
        await self._git(clone, "worktree", "add", "-b", branch, str(path), sha, mutates=True)
        metadata = {
            "workspace_id": workspace_id,
            "workdir": str(path),
            "clone": str(clone),
            "project_id": arguments["project_id"],
            "work_item_id": arguments.get("work_item_id"),
            "attempt_id": attempt,
            "repository": arguments["repository"],
            "base_sha": sha,
            "branch": branch,
            "head_sha": sha,
        }
        self._save_workspace(metadata)
        return metadata

    def _save_workspace(self, metadata: dict[str, Any]) -> None:
        with self._database() as database:
            database.execute(
                "INSERT OR REPLACE INTO workspaces(id,metadata) VALUES (?,?)",
                (metadata["workspace_id"], json.dumps(metadata)),
            )

    def _workspace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._database() as database:
            rows = database.execute("SELECT metadata FROM workspaces").fetchall()
        requested = arguments.get("workdir")
        for row in rows:
            metadata = dict(json.loads(row["metadata"]))
            matches = metadata["workspace_id"] == arguments.get("workspace_id")
            if (
                requested
                and Path(requested).expanduser().resolve() == Path(metadata["workdir"]).resolve()
            ):
                matches = True
            if not matches:
                continue
            if (
                arguments.get("workspace_id")
                and arguments["workspace_id"] != metadata["workspace_id"]
            ):
                raise ValueError("Workspace ID and workdir disagree")
            if (
                requested
                and Path(requested).expanduser().resolve() != Path(metadata["workdir"]).resolve()
            ):
                raise ValueError("Workspace ID and workdir disagree")
            if metadata["project_id"] != arguments.get("project_id") or metadata[
                "repository"
            ] != arguments.get("repository"):
                raise ValueError("Workspace belongs to a different project or repository")
            return metadata
        raise ValueError("No registered workspace matches this command")

    async def _verify_workspace(self, metadata: dict[str, Any], arguments: dict[str, Any]) -> str:
        path = Path(metadata["workdir"])
        intact = await asyncio.to_thread(
            lambda: (
                path.is_dir()
                and not path.is_symlink()
                and path.resolve().is_relative_to(self.root / "worktrees")
            )
        )
        if not intact:
            raise ValueError("Managed workspace is missing or has been replaced")
        top = (await self._git(path, "rev-parse", "--show-toplevel")).stdout.strip()
        if not await asyncio.to_thread(lambda: Path(top).resolve() == path.resolve()):
            raise ValueError("Workspace is no longer its registered Git checkout")
        branch = (await self._git(path, "branch", "--show-current")).stdout.strip()
        if branch != metadata["branch"] or arguments.get("branch", branch) != branch:
            raise ValueError("Workspace branch changed")
        head = (await self._git(path, "rev-parse", "HEAD")).stdout.strip()
        if arguments.get("expected_head_sha") and arguments["expected_head_sha"] != head:
            raise ValueError("Workspace HEAD changed since the operation was requested")
        if arguments.get("base_sha") and arguments["base_sha"] != metadata["base_sha"]:
            raise ValueError("Workspace baseline differs from the command's assigned baseline")
        if (
            await self._git(
                path, "merge-base", "--is-ancestor", metadata["base_sha"], head, check=False
            )
        ).returncode:
            raise ValueError("Workspace history no longer includes its assigned baseline")
        remote = (await self._git(path, "remote", "get-url", "origin")).stdout.strip()
        if not self._same_remote(remote, metadata["repository"]):
            raise ValueError("Workspace origin changed")
        return head

    @staticmethod
    def _policy(arguments: dict[str, Any]) -> dict[str, Any]:
        policy = arguments.get("policy", {})
        if not isinstance(policy, dict):
            raise ValueError("Project policy must be an object")
        return policy

    @staticmethod
    def _contained_file(directory: Path, relative: Path) -> bool:
        return (directory / relative).resolve().is_relative_to(directory.resolve())

    async def _deliver(self, arguments: dict[str, Any]) -> dict[str, Any]:
        metadata = self._workspace(arguments)
        path = Path(metadata["workdir"])
        pr = arguments.get("pr")
        if pr is not None:
            if not isinstance(pr, dict):
                raise ValueError("pr must be an object")
            self._text(pr, "title")
            await self._validate_ref(self._text(pr, "base"))
        async with workspace_owner(path, lock_directory=self.lock_directory):
            await self._verify_workspace(metadata, arguments)
            files = arguments.get("files")
            if files is not None:
                if (
                    not isinstance(files, list)
                    or not files
                    or not all(isinstance(item, str) and item for item in files)
                ):
                    raise ValueError(
                        "files must be an explicit, nonempty list of repository-relative paths"
                    )
                for item in files:
                    relative = Path(item)
                    if (
                        relative.is_absolute()
                        or not relative.parts
                        or ".." in relative.parts
                        or ".git" in relative.parts
                        or item.startswith(":")
                    ):
                        raise ValueError("Only explicit repository-relative files can be staged")
                    if not await asyncio.to_thread(self._contained_file, path, relative):
                        raise ValueError("File escapes its workspace")
                    if await asyncio.to_thread((path / relative).is_dir):
                        raise ValueError(
                            "List files explicitly instead of staging whole directories"
                        )
                already_staged = await self._git(path, "diff", "--cached", "--name-only", "-z")
                if set(already_staged.stdout.rstrip("\0").split("\0")) - {"", *files}:
                    raise ValueError("Unrelated staged files must be handled before this delivery")
                await self._git(path, "--literal-pathspecs", "add", "--", *files, mutates=True)
            staged = await self._git(path, "diff", "--cached", "--quiet", check=False)
            if staged.returncode not in {0, 1}:
                raise GitHubCommandError("Cannot inspect the staged changes")
            if staged.returncode == 1:
                message = self._text(arguments, "message")
                commit = ["commit"]
                if self._policy(arguments).get("require_signed_commits"):
                    commit.append("-S")
                commit.extend(["-m", message])
                await self._git(path, *commit, mutates=True)
            head = (await self._git(path, "rev-parse", "HEAD")).stdout.strip()
            if self._policy(arguments).get("require_signed_commits"):
                commits = (
                    await self._git(path, "rev-list", f"{metadata['base_sha']}..{head}")
                ).stdout.splitlines()
                for commit_sha in commits:
                    await self._git(path, "verify-commit", commit_sha)
            self._checkpoint(
                head_sha=head, branch=metadata["branch"], workspace_id=metadata["workspace_id"]
            )
            await self._verify_workspace(metadata, {**arguments, "expected_head_sha": head})
            await self._git(
                path, "push", "origin", f"{head}:refs/heads/{metadata['branch']}", mutates=True
            )
            result = {
                "head_sha": head,
                "branch": metadata["branch"],
                "workspace_id": metadata["workspace_id"],
                "workdir": str(path),
            }
            if pr:
                result["pr"] = await self._ensure_pr(arguments, metadata["branch"], head, pr)
            metadata["head_sha"] = head
            self._save_workspace(metadata)
            return result

    async def _ensure_pr(
        self, arguments: dict[str, Any], branch: str, head: str, pr: dict[str, Any]
    ) -> dict[str, Any]:
        base = self._text(pr, "base")
        await self._validate_ref(base)
        existing = await self._find_pr(arguments, branch, base, head)
        if existing:
            return existing
        body = (
            str(pr.get("body", ""))
            + f"\n\n<!-- chiliarch-command:{hashlib.sha256(self._command_id.encode()).hexdigest()} -->"
        )
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            body_file = Path(temporary) / "body.md"
            body_file.write_text(body, encoding="utf-8")
            argv = [
                "gh",
                "pr",
                "create",
                "--repo",
                self._repo_slug(arguments),
                "--head",
                branch,
                "--base",
                base,
                "--title",
                self._text(pr, "title"),
                "--body-file",
                str(body_file),
            ]
            if pr.get("draft", False):
                argv.append("--draft")
            await self._call(argv, mutates=True)
        observed = await self._find_pr(arguments, branch, base, head)
        if not observed:
            raise GitHubCommandError(
                "Pull request creation returned without an identifiable pull request"
            )
        return observed

    async def _find_pr(
        self, arguments: dict[str, Any], branch: str, base: str, head: str
    ) -> dict[str, Any] | None:
        result = await self._call(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self._repo_slug(arguments),
                "--head",
                branch,
                "--base",
                base,
                "--state",
                "all",
                "--json",
                "number,headRefOid,state,isCrossRepository",
                "--limit",
                "100",
            ]
        )
        candidates = json.loads(result.stdout)
        matches = [
            item
            for item in candidates
            if item.get("headRefOid") == head and not item.get("isCrossRepository", False)
        ]
        if len(matches) > 1:
            raise ValueError("More than one pull request matches this delivery")
        if not matches:
            return None
        return dict(
            (
                await self._inspect_pull_request(
                    {**arguments, "pr_number": matches[0]["number"], "expected_head_sha": head}
                )
            )["pr"]
        )

    async def _create_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        branch = self._text(arguments, "head")
        head = self._sha(arguments, "expected_head_sha")
        await self._validate_ref(branch)
        remote = await self._remote_ref(arguments, f"refs/heads/{branch}")
        if remote is None or remote["object"]["sha"] != head:
            raise ValueError("Pull request head branch no longer matches the expected commit")
        for dependency in arguments.get("dependency_shas", []):
            dependency = self._sha({"dependency_sha": dependency}, "dependency_sha")
            comparison = await self._api(
                arguments, self._endpoint(arguments, f"/compare/{dependency}...{head}")
            )
            if comparison.get("status") not in {"ahead", "identical"}:
                raise ValueError("Integration HEAD does not contain every delivered dependency")
        self._checkpoint(head_sha=head, branch=branch)
        return {"pr": await self._ensure_pr(arguments, branch, head, arguments)}

    @staticmethod
    def _number(arguments: dict[str, Any], key: str) -> int:
        value = arguments.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        return value

    async def _inspect_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        number = self._number(arguments, "pr_number")
        fields = "number,url,headRefOid,baseRefOid,headRefName,baseRefName,state,mergedAt,mergeCommit,statusCheckRollup,isDraft,mergeStateStatus"
        response = await self._call(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                self._repo_slug(arguments),
                "--json",
                fields,
            ]
        )
        value = json.loads(response.stdout)
        if (
            arguments.get("expected_head_sha")
            and value["headRefOid"] != arguments["expected_head_sha"]
        ):
            raise ValueError("Pull request HEAD changed")
        checks = []
        for check in value.get("statusCheckRollup") or []:
            context = check.get("__typename") == "StatusContext"
            checks.append(
                {
                    "name": check.get("context") if context else check.get("name"),
                    "status": "COMPLETED"
                    if context and check.get("state") != "PENDING"
                    else check.get("status", "PENDING"),
                    "conclusion": check.get("state") if context else check.get("conclusion"),
                    "url": check.get("targetUrl") if context else check.get("detailsUrl"),
                    "workflow": check.get("workflowName"),
                    "head_sha": value["headRefOid"],
                }
            )
        return {
            "pr": {
                "number": value["number"],
                "url": value["url"],
                "head_sha": value["headRefOid"],
                "base_sha": value["baseRefOid"],
                "head": value["headRefName"],
                "base": value["baseRefName"],
                "state": value["state"],
                "merged": bool(value.get("mergedAt")),
                "merge_commit_sha": (value.get("mergeCommit") or {}).get("oid"),
                "checks": checks,
                "draft": value.get("isDraft", False),
                "merge_state": value.get("mergeStateStatus"),
            }
        }

    async def _merge_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        head = self._sha(arguments, "head_sha")
        base = self._sha(arguments, "base_sha")
        pr = (await self._inspect_pull_request({**arguments, "expected_head_sha": head}))["pr"]
        if pr["base_sha"] != base:
            raise ValueError("Pull request base changed; refresh the integration evidence")
        if pr["merged"]:
            return {"pr": pr}
        if pr["state"] != "OPEN" or pr["draft"]:
            raise ValueError("Only an open, ready pull request can be merged")
        checks = arguments.get("required_checks", [])
        if not isinstance(checks, list) or not all(
            isinstance(item, str) and item for item in checks
        ):
            raise ValueError("required_checks must be a list of check names")
        for name in checks:
            observations = [item for item in pr["checks"] if item["name"] == name]
            if not observations or any(
                item["status"] != "COMPLETED" or item["conclusion"] != "SUCCESS"
                for item in observations
            ):
                raise ValueError("Required checks are missing, pending or unsuccessful")
        method = arguments.get("merge_method", "squash")
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError("Unsupported merge method")
        self._checkpoint(head_sha=head, base_sha=base, pr_number=pr["number"])
        await self._call(
            [
                "gh",
                "pr",
                "merge",
                str(pr["number"]),
                "--repo",
                self._repo_slug(arguments),
                "--match-head-commit",
                head,
                f"--{method}",
            ],
            mutates=True,
        )
        observed = (await self._inspect_pull_request({**arguments, "expected_head_sha": head}))[
            "pr"
        ]
        if not observed["merged"]:
            return {
                "pr": observed,
                "merge_pending": True,
                "merge_verified_head_sha": head,
                "merge_verified_base_sha": base,
            }
        return {"pr": observed, "merge_verified_head_sha": head, "merge_verified_base_sha": base}

    async def _comment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        number = self._number(
            arguments, "issue_number" if "issue_number" in arguments else "pr_number"
        )
        body = self._text(arguments, "body")
        marker = (
            "<!-- chiliarch-command:"
            + hashlib.sha256(self._command_id.encode()).hexdigest()
            + " -->"
        )
        self._checkpoint(number=number, comment_marker=marker)
        existing = await self._find_comment(arguments, number, marker)
        if existing:
            return {"comment": existing}
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            body_file = Path(temporary) / "body.md"
            body_file.write_text(body + "\n\n" + marker, encoding="utf-8")
            result = await self._call(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(number),
                    "--repo",
                    self._repo_slug(arguments),
                    "--body-file",
                    str(body_file),
                ],
                mutates=True,
            )
        return {"url": result.stdout.strip(), "comment_marker": marker}

    async def _find_comment(
        self, arguments: dict[str, Any], number: int, marker: str
    ) -> dict[str, Any] | None:
        endpoint = self._endpoint(arguments, f"/issues/{number}/comments?per_page=100")
        response = await self._call(
            [
                "gh",
                "api",
                "--hostname",
                self._repository(arguments)["host"],
                endpoint,
                "--paginate",
                "--slurp",
            ]
        )
        pages = json.loads(response.stdout)
        return next(
            (item for page in pages for item in page if marker in item.get("body", "")), None
        )

    async def _tag_commit(self, arguments: dict[str, Any], tag: str) -> str | None:
        ref = await self._remote_ref(arguments, f"refs/tags/{tag}")
        if ref is None:
            return None
        item = ref["object"]
        seen: set[str] = set()
        while item["type"] == "tag":
            if item["sha"] in seen:
                raise ValueError("Invalid cyclic annotated tag")
            seen.add(item["sha"])
            item = (
                await self._api(arguments, self._endpoint(arguments, "/git/tags/" + item["sha"]))
            )["object"]
        if item["type"] != "commit":
            raise ValueError("Release tag does not point to a commit")
        return str(item["sha"])

    async def _publish_tag(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tag = self._text(arguments, "tag")
        commit = self._sha(arguments, "commit_sha")
        await self._validate_ref(tag, tag=True)
        observed = await self._tag_commit(arguments, tag)
        if observed and observed != commit:
            raise ValueError(
                "Release tag already points to another commit; it will not be overwritten"
            )
        if observed and self._policy(arguments).get("require_signed_tags"):
            reference = await self._remote_ref(arguments, f"refs/tags/{tag}")
            assert reference is not None
            if reference["object"]["type"] != "tag":
                raise ValueError("Existing remote tag is not a signed annotated tag")
            annotated = await self._api(
                arguments, self._endpoint(arguments, "/git/tags/" + reference["object"]["sha"])
            )
            if not (annotated.get("verification") or {}).get("verified"):
                raise ValueError("Existing remote tag signature is not verified")
        self._checkpoint(tag=tag, commit_sha=commit)
        clone = await self._clone(arguments, commit)
        verification = await self._verify_release(arguments, clone, commit)
        if observed is None:
            local = await self._git(
                clone, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}", check=False
            )
            if local.returncode == 0 and local.stdout.strip() != commit:
                raise ValueError("Local release tag points to another commit")
            if local.returncode:
                signed = self._policy(arguments).get("require_signed_tags", False)
                await self._git(
                    clone,
                    "tag",
                    "-s" if signed else "-a",
                    tag,
                    commit,
                    "-m",
                    str(arguments.get("release_title") or tag),
                    mutates=True,
                )
            if self._policy(arguments).get("require_signed_tags"):
                await self._git(clone, "verify-tag", tag)
            await self._git(
                clone, "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}", mutates=True
            )
        if arguments.get("release_owner") == "daemon":
            release = await self._release(arguments, tag)
            if release is None:
                with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                    notes = Path(temporary) / "notes.md"
                    notes.write_text(str(arguments.get("release_notes", "")), encoding="utf-8")
                    await self._call(
                        [
                            "gh",
                            "release",
                            "create",
                            tag,
                            "--repo",
                            self._repo_slug(arguments),
                            "--verify-tag",
                            "--target",
                            commit,
                            "--title",
                            str(arguments.get("release_title") or tag),
                            "--notes-file",
                            str(notes),
                        ],
                        mutates=True,
                    )
        return {**await self._inspect_release(arguments), "verification": verification}

    @staticmethod
    def _argv(arguments: dict[str, Any], key: str) -> list[str]:
        values = arguments.get(key)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value and "\0" not in value for value in values)
        ):
            raise ValueError(f"{key} must be a nonempty list of command arguments")
        return values

    def _timeout(self, arguments: dict[str, Any], key: str) -> float:
        timeout = arguments.get(key, self.timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"{key} must be a positive number")
        return float(timeout)

    async def _verify_release(
        self, arguments: dict[str, Any], clone: Path, commit: str
    ) -> dict[str, Any]:
        version = self._text(arguments, "version")
        argv = [
            value.replace("{version}", version).replace("{commit_sha}", commit)
            for value in self._argv(arguments, "verify_argv")
        ]
        timeout = self._timeout(arguments, "verify_timeout_seconds")
        identity = hashlib.sha256(self._command_id.encode()).hexdigest()
        path = self.root / "release-checks" / identity
        path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint(verification_workdir=str(path))
        await self._git(clone, "worktree", "add", "--detach", str(path), commit, mutates=True)
        try:
            async with workspace_owner(path, lock_directory=self.lock_directory):
                checked = await self._call(argv, path, mutates=True, timeout_seconds=timeout)
                await self._git(path, "diff", "--quiet", "HEAD", "--")
                observed = (await self._git(path, "rev-parse", "HEAD")).stdout.strip()
                if observed != commit:
                    raise ValueError("Release verification changed the checked-out commit")
                verification = {
                    "commit_sha": commit,
                    "version": version,
                    "command": argv,
                    "exit_code": checked.returncode,
                }
                self._checkpoint(verification=verification)
                return verification
        finally:
            # Never force-remove outputs or modified files created by the verifier.
            # A non-clean verification worktree remains available for inspection.
            await self._git(clone, "worktree", "remove", str(path), mutates=True, check=False)

    async def _release(self, arguments: dict[str, Any], tag: str) -> dict[str, Any] | None:
        # Only an actual HTTP 404 establishes absence. Direct lookup also covers
        # old releases, without mistaking a pagination boundary for absence.
        value = await self._api(
            arguments,
            self._endpoint(arguments, "/releases/tags/" + quote(tag, safe="")),
            missing_ok=True,
        )
        return dict(value) if value is not None else None

    async def _inspect_release(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tag = self._text(arguments, "tag")
        commit = self._sha(arguments, "commit_sha")
        await self._validate_ref(tag, tag=True)
        observed = await self._tag_commit(arguments, tag)
        if observed and observed != commit:
            raise ValueError("Release tag does not match the expected commit")
        release = await self._release(arguments, tag)
        argv = [
            "gh",
            "run",
            "list",
            "--repo",
            self._repo_slug(arguments),
            "--commit",
            commit,
            "--json",
            "databaseId,headSha,headBranch,event,status,conclusion,url,workflowName",
            "--limit",
            "100",
        ]
        if arguments.get("workflow"):
            argv += ["--workflow", self._text(arguments, "workflow")]
        runs = json.loads((await self._call(argv)).stdout)
        runs = [
            run for run in runs if run.get("headSha") == commit and run.get("headBranch") == tag
        ]
        return {
            "tag": tag,
            "commit_sha": observed,
            "release": release,
            "workflow_runs": runs,
            "published": bool(release and not release.get("draft")),
        }

    async def _list_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        previous = arguments.get("cursor")
        newest = previous
        found = previous is None
        unseen: list[dict[str, Any]] = []
        seen: set[str] = set()
        # A first observation intentionally samples only the current page. Once
        # a cursor exists, search the entire available timeline before reporting
        # a retention gap. Never silently move past an unobserved older page.
        pages = 1 if previous is None else GITHUB_EVENTS_MAX_PAGES
        for page in range(1, pages + 1):
            events = await self._api(
                arguments,
                self._endpoint(
                    arguments, f"/events?per_page={GITHUB_EVENTS_PAGE_SIZE}&page={page}"
                ),
            )
            if page == 1 and events:
                newest = str(events[0]["id"])
            for event in events:
                identifier = str(event["id"])
                if identifier == str(previous):
                    found = True
                    break
                # A new event may shift an already read item onto the next page.
                if identifier not in seen:
                    seen.add(identifier)
                    unseen.append(event)
            if found or len(events) < GITHUB_EVENTS_PAGE_SIZE:
                break
        return {
            "events": list(reversed(unseen)),
            "cursor": newest,
            "cursor_found": found,
        }

    async def _cleanup_workspace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        metadata = self._workspace(arguments)
        path = Path(metadata["workdir"])
        async with workspace_owner(path, lock_directory=self.lock_directory):
            head = await self._verify_workspace(metadata, arguments)
            dirty = await self._git(path, "status", "--porcelain", "--untracked-files=all")
            if dirty.stdout.strip():
                raise ValueError("Workspace contains uncommitted or untracked files")
            remote = await self._remote_ref(arguments, f"refs/heads/{metadata['branch']}")
            if not remote or remote["object"]["sha"] != head:
                raise ValueError("Workspace HEAD has not been verified on its remote branch")
            self._checkpoint(workspace_id=metadata["workspace_id"], head_sha=head)
            await self._git(Path(metadata["clone"]), "worktree", "remove", str(path), mutates=True)
            with self._database() as database:
                database.execute("DELETE FROM workspaces WHERE id = ?", (metadata["workspace_id"],))
        return {
            "workspace_id": metadata["workspace_id"],
            "removed": True,
            "remote_branch_deleted": False,
        }

    async def _run_project_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        metadata = self._workspace(arguments)
        argv = self._argv(arguments, "argv")
        path = Path(metadata["workdir"])
        async with workspace_owner(path, lock_directory=self.lock_directory):
            before = await self._verify_workspace(metadata, arguments)
            self._checkpoint(workspace_id=metadata["workspace_id"], head_sha=before)
            result = await self._call(
                argv,
                path,
                mutates=True,
                check=False,
                timeout_seconds=self._timeout(arguments, "timeout_seconds"),
            )
            after = (await self._git(path, "rev-parse", "HEAD")).stdout.strip()
            return {
                "workspace_id": metadata["workspace_id"],
                "head_sha": after,
                "started_head_sha": before,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "passed": result.returncode == 0,
            }

    async def _reconcile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        original_id = self._text(arguments, "original_command_id")
        with self._database() as database:
            row = database.execute("SELECT * FROM commands WHERE id = ?", (original_id,)).fetchone()
        if row is None or original_id == self._command_id:
            raise ValueError("Original command does not exist")
        request = json.loads(row["request"])
        original_arguments = request["arguments"]
        if arguments.get("project_id") and arguments["project_id"] != original_arguments.get(
            "project_id"
        ):
            raise ValueError("Original command belongs to another project")
        if row["state"] in {"completed", "failed"}:
            original = json.loads(row["receipt"])
            return {
                "original_command_id": original_id,
                "original_status": row["state"],
                "original_result": original["result"],
                "original_error": original["error"],
                "receipt": original,
            }
        progress = json.loads(row["progress"])
        await self._verify_identity(original_arguments)
        action = request["action"]
        result: dict[str, Any] | None = None
        if action == "deliver" and progress.get("head_sha"):
            head, branch = progress["head_sha"], progress["branch"]
            remote = await self._remote_ref(original_arguments, f"refs/heads/{branch}")
            if remote and remote["object"]["sha"] == head:
                result = {
                    "head_sha": head,
                    "branch": branch,
                    "workspace_id": progress.get("workspace_id"),
                }
                if original_arguments.get("pr"):
                    pr = await self._find_pr(
                        original_arguments, branch, original_arguments["pr"]["base"], head
                    )
                    if pr:
                        result["pr"] = pr
                    else:
                        result = None
        elif action == "create_pull_request":
            pr = await self._find_pr(
                original_arguments,
                original_arguments["head"],
                original_arguments["base"],
                original_arguments["expected_head_sha"],
            )
            if pr:
                result = {"pr": pr}
        elif action == "merge_pull_request":
            observed = (
                await self._inspect_pull_request(
                    {**original_arguments, "expected_head_sha": original_arguments["head_sha"]}
                )
            )["pr"]
            if observed["merged"]:
                result = {
                    "pr": observed,
                    "merge_verified_head_sha": progress.get("head_sha"),
                    "merge_verified_base_sha": progress.get("base_sha"),
                }
        elif action == "publish_tag":
            observed = await self._inspect_release(original_arguments)
            if (
                observed["commit_sha"] == original_arguments["commit_sha"]
                and progress.get("verification", {}).get("commit_sha") == observed["commit_sha"]
            ):
                if original_arguments.get("release_owner") != "daemon" or observed["release"]:
                    result = {**observed, "verification": progress["verification"]}
        elif action in {"comment", "comment_issue"} and progress.get("comment_marker"):
            comment = await self._find_comment(
                original_arguments, progress["number"], progress["comment_marker"]
            )
            if comment:
                result = {"comment": comment}
        elif action == "update_issue" and progress.get("desired_issue"):
            issue = (await self._inspect_issue(original_arguments))["issue"]
            if self._issue_matches(issue, progress["desired_issue"]):
                result = {"issue": issue, "changed": True}
        elif action == "prepare_workspace":
            try:
                metadata = self._workspace(
                    {**original_arguments, "workspace_id": progress.get("workspace_id")}
                )
                await self._verify_workspace(metadata, original_arguments)
                result = metadata
            except ValueError:
                pass
        elif action == "cleanup_workspace" and progress.get("workspace_id"):
            with self._database() as database:
                exists = database.execute(
                    "SELECT 1 FROM workspaces WHERE id = ?", (progress["workspace_id"],)
                ).fetchone()
            if not exists:
                result = {
                    "workspace_id": progress["workspace_id"],
                    "removed": True,
                    "remote_branch_deleted": False,
                }
        if result is None:
            return {
                "original_command_id": original_id,
                "original_status": "unknown",
                "original_result": progress,
                "original_error": "The original result has not been established",
                "requires_inspection": True,
            }
        receipt = self._receipt(original_id, "completed", result)
        self._save_receipt(receipt)
        return {
            "original_command_id": original_id,
            "original_status": "completed",
            "original_result": result,
            "original_error": None,
            "receipt": receipt,
        }
