from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import clippers_daily.app as app
import clippers_daily.notes as notes


def test_git_retry_recovers_and_preserves_stderr(monkeypatch):
    results = [
        SimpleNamespace(returncode=1, stdout="", stderr="temporary network failure"),
        SimpleNamespace(returncode=1, stdout="", stderr="temporary network failure"),
        SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    ]
    calls = []
    monkeypatch.setattr(notes, "_git", lambda repo, *args, check=False: calls.append(args) or results.pop(0))
    monkeypatch.setattr(notes.time, "sleep", lambda seconds: None)
    notes._git_with_retry(Path("/tmp/repo"), "push", "origin", "main", attempts=3, delay_seconds=0)
    assert len(calls) == 3


def test_notes_failure_is_logged_but_not_raised(monkeypatch):
    logged = []
    store = SimpleNamespace(log=lambda *args: logged.append(args))
    digest = SimpleNamespace(date="2026-08-15")
    monkeypatch.setattr(app, "publish_daily_inbox", lambda *args: (_ for _ in ()).throw(RuntimeError("ssh timeout")))
    app._publish_notes_best_effort(store, "run-1", "daily", digest, [], {})
    assert logged[0][1:4] == ("warning", "git", "日报笔记同步失败，继续发送邮件")
    assert logged[0][4]["error"] == "ssh timeout"
