"""The `sorge-label` task profile, and the `full` profile's immunity to it.

Every test here comes in pairs: one pins the narrowed behavior, its sibling
pins that the same input under `ROBOMP_TASK_PROFILE=full` still does exactly
what it did before the profile existed.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from omp_rpc import HostToolContext
from pydantic import ValidationError

from robomp import host_tools
from robomp.config import Settings
from robomp.db import Database
from robomp.github_client import GitHubClient, IssueInfo, RepoInfo
from robomp.github_events import route
from robomp.host_tools import ToolBindings
from robomp.sandbox import LocalGitTransport, Workspace

ALLOWLIST = frozenset({"octo/widget"})
BOT = "robomp-bot"
SELF_LOGINS = frozenset({"junghan0611"})
LABEL_ONLY = "sorge-label"


def _issue_payload(action: str, *, sender: str, author: str = "alice") -> dict[str, Any]:
    return {
        "action": action,
        "issue": {"number": 7, "user": {"login": author}, "author_association": "OWNER"},
        "repository": {"full_name": "octo/widget"},
        "sender": {"login": sender},
    }


def _comment_payload(sender: str) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"number": 7, "user": {"login": "alice"}},
        "comment": {"body": "any news?", "user": {"login": sender}},
        "repository": {"full_name": "octo/widget"},
        "sender": {"login": sender},
    }


def _route(payload: dict[str, Any], *, event: str = "issues", profile: str) -> Any:
    return route(
        event,
        payload,
        allowlist=ALLOWLIST,
        bot_login=BOT,
        task_profile=profile,
        self_logins=SELF_LOGINS,
    )


# ---------------------------------------------------------------------------
# (a)/(e) wake set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["edited", "labeled"])
def test_label_only_wakes_a_triage_turn_on_issue_edited_and_labeled(action: str) -> None:
    decision = _route(_issue_payload(action, sender="carol"), profile=LABEL_ONLY)
    assert decision.should_queue
    assert decision.task == "triage_issue"
    assert decision.issue_key == "octo/widget#7"


@pytest.mark.parametrize("action", ["edited", "labeled"])
def test_full_profile_still_ignores_issue_edited_and_labeled(action: str) -> None:
    decision = _route(_issue_payload(action, sender="carol"), profile="full")
    assert not decision.should_queue
    assert decision.task is None


@pytest.mark.parametrize("action", ["opened", "reopened"])
def test_label_only_keeps_waking_on_open_and_reopen(action: str) -> None:
    decision = _route(_issue_payload(action, sender="carol"), profile=LABEL_ONLY)
    assert decision.should_queue
    assert decision.task == "triage_issue"


# ---------------------------------------------------------------------------
# (b)/(e) self-caused deliveries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["opened", "labeled", "edited", "reopened"])
@pytest.mark.parametrize("sender", ["junghan0611", "JungHan0611", BOT])
def test_label_only_never_wakes_on_its_own_action(action: str, sender: str) -> None:
    # The label a turn just applied comes back as a `labeled` delivery whose
    # sender is us. Waking on it is an infinite loop.
    decision = _route(_issue_payload(action, sender=sender), profile=LABEL_ONLY)
    assert not decision.should_queue
    assert decision.task is None
    assert "self" in decision.reason


def test_full_profile_still_queues_an_issue_opened_by_a_self_login() -> None:
    # `full` has no notion of self-caused deliveries: a maintainer opening an
    # issue from the PAT account must still be triaged.
    decision = _route(_issue_payload("opened", sender="junghan0611"), profile="full")
    assert decision.should_queue
    assert decision.task == "triage_issue"


def test_label_only_still_cleans_up_after_its_own_close() -> None:
    # Cleanup opens no model turn, so the self-guard must not swallow it —
    # otherwise every issue we close leaks its worktree.
    decision = _route(_issue_payload("closed", sender="junghan0611"), profile=LABEL_ONLY)
    assert decision.should_queue
    assert decision.task == "cleanup_workspace"


# ---------------------------------------------------------------------------
# Non-issue events cannot open a turn the label-only toolset cannot serve
# ---------------------------------------------------------------------------


def test_label_only_drops_comment_events() -> None:
    decision = _route(_comment_payload("carol"), event="issue_comment", profile=LABEL_ONLY)
    assert not decision.should_queue
    assert decision.task is None


def test_full_profile_still_queues_comment_events() -> None:
    decision = _route(_comment_payload("carol"), event="issue_comment", profile="full")
    assert decision.should_queue
    assert decision.task == "handle_comment"


def test_label_only_drops_incoming_pr_review() -> None:
    payload = {
        "action": "opened",
        "pull_request": {"number": 11, "state": "open", "user": {"login": "carol"}},
        "repository": {"full_name": "octo/widget"},
        "sender": {"login": "carol"},
    }
    decision = _route(payload, event="pull_request", profile=LABEL_ONLY)
    assert not decision.should_queue
    assert decision.task is None


def test_full_profile_still_queues_incoming_pr_review() -> None:
    payload = {
        "action": "opened",
        "pull_request": {"number": 11, "state": "open", "user": {"login": "carol"}},
        "repository": {"full_name": "octo/widget"},
        "sender": {"login": "carol"},
    }
    decision = _route(payload, event="pull_request", profile="full")
    assert decision.should_queue
    assert decision.task == "review_pr"


# ---------------------------------------------------------------------------
# (c)/(e) per-issue queue coalescing
# ---------------------------------------------------------------------------


def _queue_three_label_events(db: Database) -> list[str]:
    ids = []
    for n in range(3):
        delivery = f"d{n}"
        db.record_event(
            delivery_id=delivery,
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#7",
            payload={"action": "labeled", "n": n},
        )
        ids.append(delivery)
    return ids


def test_label_only_claim_collapses_an_issue_backlog_to_its_newest_delivery(db: Database) -> None:
    first, second, third = _queue_three_label_events(db)

    claimed = db.claim_next_event(coalesce_issue_events=True)

    assert claimed is not None
    assert claimed.delivery_id == third, "the newest verdict request must be the one that runs"
    assert db.get_event(first).state == "skipped"
    assert db.get_event(second).state == "skipped"
    assert db.claim_next_event(coalesce_issue_events=True) is None


def test_full_profile_claim_runs_every_queued_delivery_in_order(db: Database) -> None:
    first, second, third = _queue_three_label_events(db)

    claimed = db.claim_next_event()

    assert claimed is not None
    assert claimed.delivery_id == first
    assert db.get_event(second).state == "queued"
    assert db.get_event(third).state == "queued"


def test_coalescing_leaves_other_issues_alone(db: Database) -> None:
    db.record_event(
        delivery_id="other",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#99",
        payload={"action": "labeled"},
    )
    _queue_three_label_events(db)

    claimed = db.claim_next_event(coalesce_issue_events=True)

    assert claimed is not None
    assert claimed.delivery_id == "other"
    assert [db.get_event(f"d{n}").state for n in range(3)] == ["queued", "queued", "queued"]


# ---------------------------------------------------------------------------
# (d)/(e) host toolset
# ---------------------------------------------------------------------------


def _bindings(db: Database, tmp_path: Path, cfg: Settings) -> tuple[ToolBindings, asyncio.AbstractEventLoop]:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    session_dir = root / ".omp-session"
    context_dir = root / "context"
    artifacts_dir = root / "artifacts"
    for p in (root, repo_dir, session_dir, context_dir, context_dir / "repro", artifacts_dir):
        p.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=session_dir,
        context_dir=context_dir,
        artifacts_dir=artifacts_dir,
        branch="farm/abc12345/some-issue",
        repo_full_name="octo/widget",
        issue_number=7,
    )
    loop = asyncio.new_event_loop()
    bindings = ToolBindings(
        db=db,
        github=GitHubClient("token", transport=httpx.MockTransport(lambda _r: httpx.Response(500))),
        git_transport=LocalGitTransport(token=None),
        repo=RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://x/octo/widget.git",
            private=False,
        ),
        issue=IssueInfo(
            repo="octo/widget",
            number=7,
            title="boom",
            body="b",
            state="open",
            author="alice",
            labels=("house:sorge", "state:ready"),
            is_pull_request=False,
        ),
        workspace=workspace,
        loop=loop,
        settings=cfg,
        author_name="robomp-bot",
        author_email="robomp-bot@example.invalid",
    )
    return bindings, loop


def test_label_only_toolset_is_exactly_read_label_abort(db: Database, tmp_path: Path, settings: Settings) -> None:
    bindings, loop = _bindings(db, tmp_path, settings.model_copy(update={"task_profile": LABEL_ONLY}))
    try:
        names = {tool.name for tool in host_tools.build(bindings)}
    finally:
        loop.close()
    assert names == {"set_issue_labels", "fetch_issue_thread", "gh_search_issues", "abort_task"}


def test_full_profile_toolset_still_exposes_the_mutating_tools(
    db: Database, tmp_path: Path, settings: Settings
) -> None:
    bindings, loop = _bindings(db, tmp_path, settings)
    try:
        names = {tool.name for tool in host_tools.build(bindings)}
    finally:
        loop.close()
    assert {"classify_issue", "gh_post_comment", "gh_push_branch", "gh_open_pr", "submit_pr_review"} <= names


def test_label_only_replaces_the_previous_value_of_a_single_value_axis(
    db: Database, tmp_path: Path, settings: Settings
) -> None:
    # GitHub's label endpoint is additive; a `state:` re-verdict that left the
    # old value behind would make the axis meaningless.
    removed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/labels"):
            return httpx.Response(
                200,
                json=[{"name": "house:sorge"}, {"name": "state:ready"}, {"name": "state:running"}],
            )
        if request.method == "DELETE":
            removed.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=[])
        return httpx.Response(500, json={"message": "unexpected"})

    cfg = settings.model_copy(update={"task_profile": LABEL_ONLY})
    bindings, loop = _bindings(db, tmp_path, cfg)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    object.__setattr__(bindings, "github", GitHubClient("token", transport=httpx.MockTransport(handler)))
    try:
        tool = next(t for t in host_tools.build(bindings) if t.name == "set_issue_labels")
        result = tool.execute(
            {"labels": ["state:running", "house:sorge"]},
            HostToolContext(tool_call_id="tc-1", _cancel_event=threading.Event(), _send_update=lambda _p: None),
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()

    assert removed == ["state:ready"]
    assert "state:running" in result
    assert "state:ready" not in result


# ---------------------------------------------------------------------------
# Configuration contract
# ---------------------------------------------------------------------------


def test_unknown_task_profile_fails_at_startup(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOMP_TASK_PROFILE", "label-only")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_task_profile_tolerates_env_file_padding(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOMP_TASK_PROFILE", " Sorge-Label ")
    assert Settings().sorge_label_only is True  # type: ignore[call-arg]


def test_default_profile_is_full(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # `_env_file=None`: an operator's runtime `.env` in the repo must not
    # decide what this test calls the default.
    monkeypatch.delenv("ROBOMP_TASK_PROFILE", raising=False)
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.task_profile == "full"
    assert cfg.sorge_label_only is False
    assert cfg.self_logins == frozenset()
    assert cfg.agent_dir is None


def test_self_logins_are_normalized(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOMP_SELF_LOGINS", " @JungHan0611 , sorge-bot[bot] ,, ")
    assert Settings().self_logins == frozenset({"junghan0611", "sorge-bot"})  # type: ignore[call-arg]


def test_relative_agent_dir_fails_at_startup(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # A relative path would resolve against the child's per-issue worktree.
    monkeypatch.setenv("ROBOMP_AGENT_DIR", "./agent")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
