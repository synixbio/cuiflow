from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cuiflow.core.manifests import git_commit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    (root / "README").write_text("x", encoding="utf-8")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "init")
    return git("rev-parse", "HEAD")


def test_git_commit_of_a_cuiflow_checkout(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "cuiflow"
    pkg.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "cuiflow"\n', encoding="utf-8")
    head = _repo(tmp_path)
    assert git_commit(pkg) == head


def test_git_commit_ignores_the_repository_cuiflow_is_installed_in(tmp_path: Path) -> None:
    _repo(tmp_path)  # a user's project, with a virtualenv inside it
    site = tmp_path / ".venv" / "Lib" / "site-packages" / "cuiflow"
    site.mkdir(parents=True)
    assert git_commit(site) is None
    # Even a src/ layout counts only when the project is cuiflow itself ...
    other = tmp_path / "src" / "cuiflow"
    other.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n', encoding="utf-8")
    assert git_commit(other) is None
    # ... and its root is the top of the work tree.
    nested = tmp_path / "vendor" / "src" / "cuiflow"
    nested.mkdir(parents=True)
    (tmp_path / "vendor" / "pyproject.toml").write_text('name = "cuiflow"\n', encoding="utf-8")
    assert git_commit(nested) is None
