"""Samples, fixtures and tests use made-up project names only, from one list: tests/demo-names.json.

Why: on 2026-11-12 a sample row for "what a real screen held" was given the project name seen in that real account.
It was a customer's name, in a public repository. A scan for customers' names needs to know them. This goes the
other way: a project name that is not on the list of demo names fails, whoever's it is.

Two checks. The picture check's sample rows are generated and every row is read (the name there was passed to a
helper by position, where no pattern would have seen it). And every sample, fixture and test file is searched for a
project given by name."""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
NAMES = json.loads((ROOT / "tests" / "demo-names.json").read_text())
IDS, LABELS = set(NAMES["ids"]), set(NAMES["labels"])
VIEWS = ("empty", "leftover", "stopped", "one", "several", "failed", "found", "model")
# A project given by name: project: "x", "project_name": "X", project_id='x'. Not a path, a sentence or a variable.
GIVEN = re.compile(r'''\bproject(?:_name|Name|_id|_label)?["']?\s*[:=]\s*["']([A-Za-z0-9][A-Za-z0-9 _.-]{0,40})["']''')


def _files():
    out = [*ROOT.glob("tests/mod-render/*.mjs"), *ROOT.glob("tests/mod-render/*.py"), *ROOT.glob("tests/**/*.json"), *ROOT.glob("backend/core/tests/*.py"),
           *ROOT.glob("defaults/mods/*/demo*"), *ROOT.glob("defaults/mods/*/samples/*")]
    return [f for f in out if f.is_file() and f.name not in ("demo-names.json", "test_demo_names.py", "demo-names.test.mjs")]


def test_the_sample_rows_for_pictures_use_demo_names_only():
    bad = []
    for view in VIEWS:
        got = subprocess.run([sys.executable, str(ROOT / "tests" / "mod-render" / "deployments-demo.py"), view], capture_output=True, text=True, check=True)
        for r in json.loads(got.stdout)["demo"]["deployed"]["rows"]:
            pid, label, version = r.get("project"), r.get("project_name"), r.get("model_version")
            if pid not in IDS:
                bad.append(f"{view}: project {pid!r}")
            if label is not None and label not in LABELS:
                bad.append(f"{view}: project name {label!r}")
            if version and version.split("/", 1)[0] not in IDS:
                bad.append(f"{view}: model version {version!r}")
            if r.get("link") != "unlinked" and r.get("name") and pid and pid not in str(r["name"]):
                bad.append(f"{view}: the cloud name {r['name']!r} is not made from the project {pid!r}")
    assert not bad, "Not on the list of demo names (tests/demo-names.json): " + "; ".join(bad)


def test_no_sample_fixture_or_test_gives_a_project_a_name_off_the_list():
    bad = []
    for f in _files():
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for name in GIVEN.findall(line):
                if name not in IDS and name not in LABELS:
                    bad.append(f"{f.relative_to(ROOT)}:{i}: {name!r}")
    assert not bad, ("A project name that is not on the list of demo names (tests/demo-names.json). Use one from the list, or make one up and "
                     "add it. Never a name from a real account: " + "; ".join(bad[:20]))


def test_the_check_itself_catches_a_name_that_is_not_on_the_list():
    assert GIVEN.findall('row({ project: "real-customer", project_name: "Real Customer" })') == ["real-customer", "Real Customer"]
    assert "real-customer" not in IDS and "Real Customer" not in LABELS
    assert GIVEN.findall('"project": "shop"') == ["shop"] and "shop" in IDS
    assert GIVEN.findall("project = ctx.project") == [] and GIVEN.findall('project: "a/path/like/this"') == []
