"""cst view new: scaffold a team view from the template Captain's app uses."""
import json
from pathlib import Path

from typer.testing import CliRunner

from captain_search import extensions as X
from captain_search.cli.main import app

REPO_TEMPLATE = Path(__file__).resolve().parents[3] / "defaults" / "captain" / "view-template"


def test_scaffolds_a_view_in_the_project(tmp_path, monkeypatch):
    proj = X.scaffold(str(tmp_path / "repo"), "acme")
    monkeypatch.setenv("CST_PROJECT", str(proj))
    r = CliRunner().invoke(app, ["--json", "view", "new", "query-health", "--icon", "chart"])
    assert r.exit_code == 0, r.stdout
    d = proj / "views" / "query-health"
    mod = json.loads((d / "mod.json").read_text())
    assert mod["title"] == "Query health" and mod["icon"] == "chart" and mod["captain"]["view_api"] == 1
    assert "{{" not in (d / "main.js").read_text() and not (d / "kit").exists()
    assert "kit/" in (d / ".gitignore").read_text().splitlines()
    again = CliRunner().invoke(app, ["view", "new", "query-health"])
    assert again.exit_code == 1 and "already exists" in again.output
    bad = CliRunner().invoke(app, ["view", "new", "Bad Name"])
    assert bad.exit_code == 1 and "can't be a view name" in bad.output


def test_needs_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_PROJECT", str(tmp_path / "nothing"))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["view", "new", "x"])
    assert r.exit_code == 1 and "cst project init" in r.output


def test_package_template_matches_the_apps(tmp_path):
    """The template ships inside cst (for installs without the app repo) and must stay identical to the app's."""
    if not REPO_TEMPLATE.is_dir():
        return
    pkg = Path(__file__).resolve().parents[1] / "captain_search" / "view_template"
    for f in ("mod.json", "main.js", "README.md", ".gitignore"):
        assert (pkg / f).read_text() == (REPO_TEMPLATE / f).read_text(), f"{f} drifted: copy defaults/captain/view-template again"
