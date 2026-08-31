from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import runpy
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONTROLLER = REPO_ROOT / "apps" / "insta360_auto_converter.py"


def _load_legacy_module():
    spec = importlib.util.spec_from_file_location(
        "legacy_controller_under_test", LEGACY_CONTROLLER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_is_inert(capsys):
    module = _load_legacy_module()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "disabled" in module.LEGACY_DISABLED_MESSAGE.lower()


def test_direct_entry_point_fails_closed_without_external_actions(
    monkeypatch, capsys
):
    def blocked(*_args, **_kwargs):
        raise AssertionError("retired controller attempted an external side effect")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ("remove", "rename", "replace", "system", "unlink"):
        monkeypatch.setattr(os, name, blocked)
    for name in ("touch", "unlink", "rename", "replace", "write_text", "write_bytes"):
        monkeypatch.setattr(Path, name, blocked)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(LEGACY_CONTROLLER), run_name="__main__")

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "legacy Google Drive/Docker controller is disabled" in captured.err


def test_legacy_controller_contains_no_process_or_cleanup_primitives():
    tree = ast.parse(LEGACY_CONTROLLER.read_text(encoding="utf-8"))

    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imported_roots.isdisjoint(
        {"glob", "os", "pathlib", "shutil", "subprocess", "urllib"}
    )
    assert not any(
        keyword.arg == "shell"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    )
