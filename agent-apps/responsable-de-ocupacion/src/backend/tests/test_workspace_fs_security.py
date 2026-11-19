"""Security-focused tests for confined workspace path resolution."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import HTTPException


@pytest.fixture
def ws_root(monkeypatch):
    td = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", td)
    open(os.path.join(td, "safe.txt"), "w").close()
    yield td


def test_reject_parent_traversal(ws_root) -> None:
    from app.workspace_fs import resolve

    with pytest.raises(HTTPException) as exc:
        resolve("../outside")
    assert exc.value.detail["code"] == "workspace.invalid_path"


def test_missing_path_not_found(ws_root) -> None:
    from app.workspace_fs import resolve

    with pytest.raises(HTTPException) as exc:
        resolve("does/not/exist.txt")
    assert exc.value.detail["code"] == "workspace.not_found"


def test_valid_relative_path(ws_root) -> None:
    from app.workspace_fs import resolve

    path = resolve("safe.txt")
    assert path.endswith("safe.txt")
    assert os.path.isfile(path)


def test_list_dir_unavailable_without_mount(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_WORKSPACE_DIR", raising=False)
    from app.workspace_fs import list_dir

    out = list_dir("")
    assert out["available"] is False
    assert out["entries"] == []
