"""Set up with an agent, as it runs for real: the agent's shell can have an older cst first on PATH, no repo checkout
to read docs from, and a person who says "put that in" in chat instead of pressing Accept."""
import os
import stat

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from captain_search import codeagent
from captain_search import investigations as INV
from captain_search import project_assist as PA
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import proposals
from captain_search.cli.main import app
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.workspace import init_workspace

AGENT_ENV = ("CST_AGENT", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID", "CODEX_SANDBOX_NETWORK_DISABLED", "CURSOR_AGENT")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    for k in AGENT_ENV:  # these tests run under coding agents too: start as a person
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(proposals, "DETECT_AGENT", True)
    ctx = init_workspace("t", str(tmp_path / "store"))
    snapshot(ctx, "shop-catalog", pa.table({"id": ["a", "b"], "title": ["red hat", "blue cap"]}),
             kind=DatasetKind.catalog, provenance=Provenance(source="generated"))
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    return ctx


@pytest.fixture
def own_cst(tmp_path, monkeypatch):
    """A stand-in for the cst the services run as, at a path that is not on PATH."""
    cst = tmp_path / "Captain.app" / "Contents" / "Resources" / "cst" / "bin" / "cst"
    cst.parent.mkdir(parents=True)
    cst.write_text("#!/bin/sh\n")
    cst.chmod(cst.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CST_BIN", str(cst))
    return str(cst)


def _proposal(ctx, monkeypatch, slots):
    monkeypatch.setenv("CLAUDECODE", "1")
    a = PA.assist(ctx, "shop")
    p = PA.propose(ctx, "shop", {"note": "n", "slots": slots}, a["assist"])
    monkeypatch.delenv("CLAUDECODE")
    return p


# ---- which cst ------------------------------------------------------------------------------------


def test_cst_path_is_this_install_not_the_first_on_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CST_BIN", raising=False)
    old = tmp_path / "old" / "cst"
    old.parent.mkdir()
    old.write_text("#!/bin/sh\nexit 2\n")
    old.chmod(0o755)
    monkeypatch.setenv("PATH", f"{old.parent}{os.pathsep}{os.environ['PATH']}")
    got = codeagent.cst_path()
    assert os.path.isabs(got) and got != str(old)
    assert os.path.dirname(got) == os.path.dirname(codeagent.sys.executable)  # the script beside this python
    # The packaged app: python is Resources/cst/python/bin/python3, the launcher is Resources/cst/bin/cst.
    root = tmp_path / "Captain.app" / "Contents" / "Resources" / "cst"
    (root / "python" / "bin").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "cst").write_text("#!/bin/sh\n")
    (root / "bin" / "cst").chmod(0o755)
    monkeypatch.setattr(codeagent.sys, "executable", str(root / "python" / "bin" / "python3"))
    monkeypatch.setattr(codeagent.sys, "argv", ["cst", "serve"])  # what the launcher sets
    assert codeagent.cst_path() == str(root / "bin" / "cst")
    monkeypatch.setenv("CST_BIN", str(old))  # the app's own choice wins
    assert codeagent.cst_path() == str(old)


def test_the_assist_prompt_names_the_cst_and_no_missing_file(ws, own_cst):
    p = PA.assist(ws, "shop")["prompt"]
    first = p.splitlines()[0]
    assert first == f"Use this cst: {own_cst}"
    assert "old copy" in p.split("\n\n")[0]
    assert f"{own_cst} projects propose shop --file proposal.yaml --assist assist-" in p
    # No file path the agent's folder doesn't have: the contract is a command, and its outline is in the prompt.
    assert "docs/trainer-contract.md" not in p and ".md" not in p
    assert f"{own_cst} docs trainer-contract" in p
    for word in ("CST_TRAIN_INPUTS", "CST_PARAMS", "CST_OUT", "result.yaml", "metrics", "artifact_dir", "registry_ref",
                 "{path, uri, bytes}", "objects/sha256", "data/part-0.parquet"):
        assert word in p, word
    assert "Propose first. When the person says to apply it" in p and "Never apply on your own" in p
    assert f"{own_cst} projects accept shop <proposal id> --as-asked" in p and "press Accept in Captain" in p
    assert "stay with the person" in p


def test_other_agent_prompts_name_the_cst(ws, own_cst):
    d = INV.create(ws, "Why do numeric queries regress?")
    assert INV.prompt(ws, d["id"]).splitlines()[0] == f"Use this cst: {own_cst}"
    info = codeagent.prepare(ws)
    assert info["env"]["CST_BIN"] == own_cst and info["env"]["PATH"].split(os.pathsep)[0] == os.path.dirname(own_cst)
    guide = open(os.path.join(info["cwd"], "AGENTS.md")).read()
    assert f"Use this cst: `{own_cst}`" in guide and "press Accept in Captain" in guide
    assert "Propose first. When the person says to apply it" in guide and "--as-asked" in guide and "Never apply on your\n  own" in guide
    assert "are the person's, in the app" in guide


# ---- the contract is a command --------------------------------------------------------------------


def test_docs_prints_the_trainer_contract(ws):
    run = CliRunner()
    r = run.invoke(app, ["docs", "trainer-contract"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("# The trainer contract") and "CST_TRAIN_INPUTS" in r.output and "An input's files" in r.output
    assert "trainer-contract" in run.invoke(app, ["docs"]).output
    r = run.invoke(app, ["docs", "nope"])
    assert r.exit_code == 1 and "trainer-contract" in r.output


def test_the_contract_ships_in_the_package():
    from importlib import resources

    import tomllib

    assert resources.files("captain_search").joinpath("docs", "trainer-contract.md").is_file()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "pyproject.toml"), "rb") as f:
        data = tomllib.load(f)["tool"]["setuptools"]["package-data"]["captain_search"]
    assert "docs/*.md" in data  # a wheel (what the app packages) carries it too


# ---- accept over work already done by hand -------------------------------------------------------


def test_accept_after_the_slots_were_added_by_hand(ws, monkeypatch):
    p = _proposal(ws, monkeypatch, [
        {"slot": "sessions", "add": True, "label": "Sessions", "role": "custom", "why": "for features"},
        {"slot": "items2", "add": True, "label": "More items", "role": "catalog", "accepts": ["catalog"],
         "source": {"from": "dataset", "ref": "shop-catalog@v1"}, "why": "a second catalog"},
        {"slot": "promos", "add": True, "label": "Promos", "role": "custom", "why": "new"}])
    PS.slot_add(ws, "shop", {"id": "sessions", "label": "Sessions"})                     # the same definition
    PS.slot_add(ws, "shop", {"id": "items2", "label": "Items", "role": "events", "accepts": ["events"]})  # a real conflict
    r = PA.accept(ws, "shop", p["id"])
    a = r["applied"]
    assert a["already"] == ["sessions"] and a["added"] == ["promos"]
    assert [c["slot"] for c in a["conflicts"]] == ["items2"]
    assert "role events here, catalog proposed" in a["conflicts"][0]["reason"] and "accepts" in a["conflicts"][0]["reason"]
    assert a["pinned"] == []  # the conflicting slot's source is left out, and its slot is untouched
    slots = {s["id"]: s for s in r["state"]["slots"]}
    assert slots["items2"]["role"] == "events" and "promos" in slots
    assert r["proposal"]["status"] == "accepted" and r["proposal"]["applied"]["already"] == ["sessions"]


def test_accept_twice_applied_by_hand_still_pins_the_source(ws, monkeypatch):
    p = _proposal(ws, monkeypatch, [{"slot": "extra", "add": True, "label": "Extra", "role": "catalog",
                                     "accepts": ["catalog"], "source": {"from": "dataset", "ref": "shop-catalog@v1"}, "why": "w"}])
    PS.slot_add(ws, "shop", {"id": "extra", "label": "Extra catalog", "role": "catalog", "accepts": ["catalog"]})
    r = PA.accept(ws, "shop", p["id"])
    assert r["applied"]["already"] == ["extra"] and r["applied"]["conflicts"] == []
    assert r["applied"]["pinned"] == [{"slot": "extra", "ref": "shop-catalog@v1"}]


def test_accept_reports_a_slot_that_is_gone_and_applies_the_rest(ws, monkeypatch):
    p = _proposal(ws, monkeypatch, [{"slot": "queries", "label": "Search terms", "why": "rename"},
                                    {"slot": "catalog", "source": {"from": "dataset", "ref": "shop-catalog@v1"}, "why": "w"}])
    PS.slot_remove(ws, "shop", "queries")
    r = PA.accept(ws, "shop", p["id"])
    assert [c["slot"] for c in r["applied"]["conflicts"]] == ["queries"]
    assert r["applied"]["pinned"] == [{"slot": "catalog", "ref": "shop-catalog@v1"}]


# ---- "put that in", said to the agent --------------------------------------------------------------


def test_an_agent_applies_only_on_the_persons_word_and_it_is_recorded(ws, monkeypatch):
    run = CliRunner()
    cli = lambda *a: run.invoke(app, ["--json", "-w", "t", "projects", *a])
    p = _proposal(ws, monkeypatch, [{"slot": "sessions", "add": True, "label": "Sessions", "why": "w"},
                                    {"slot": "catalog", "add": False, "source": {"from": "dataset", "ref": "shop-catalog@v1"}, "why": "w"}])
    monkeypatch.setenv("CLAUDECODE", "1")
    # Without the person's words: refused, with what to do next. Nothing changed.
    for extra in ((), ("--as-asked", ""), ("--as-asked", " ")):
        r = cli("accept", "shop", p["id"], *extra)
        assert r.exit_code == 1, r.output
        assert "only when the person asked" in r.output and "--as-asked" in r.output and "press Accept in Captain" in r.output
    assert "sessions" not in [s["id"] for s in PS.get(ws, "shop")["slots"]] and PS.get(ws, "shop")["pins"]["catalog"] is None
    assert PA._load(ws, "shop", p["id"])[0]["status"] == "proposed"
    # An agent still can't reject: that is the person's.
    assert cli("reject", "shop", p["id"]).exit_code == 1
    # With their words: applied, and the proposal and the project's history say who, on what instruction, and when.
    r = cli("accept", "shop", p["id"], "--as-asked", "ok put that in")
    assert r.exit_code == 0, r.output
    done = PA._load(ws, "shop", p["id"])[0]
    assert done["status"] == "accepted" and done["applied_by"] == "claude" and done["on_instruction"] == "ok put that in"
    assert done["applied_at"] and done["history"][-1]["note"] == "on instruction: ok put that in"
    doc = PS.get(ws, "shop")
    assert "sessions" in [s["id"] for s in doc["slots"]] and doc["pins"]["catalog"] == "shop-catalog@v1"
    h = doc["history"][-1]
    assert h["applied_by"] == "claude" and h["on_instruction"] == "ok put that in" and h["proposal"] == p["id"] and h["at"]
    assert h["what"] == f"applied proposal {p['id']}" and h["user"] == "tester@captain.dev"
    assert cli("accept", "shop", p["id"], "--as-asked", "again").exit_code == 1  # once
    # The project commands work for an agent too, proposal open or not, and each one is written down.
    p2 = _proposal(ws, monkeypatch, [{"slot": "promos", "add": True, "label": "Promos", "why": "w"}])
    monkeypatch.setenv("CLAUDECODE", "1")
    n = len(PS.get(ws, "shop")["history"])
    assert cli("slot-add", "shop", "Returns").exit_code == 0
    assert cli("pin", "shop", "--catalog", "shop-catalog@v1").exit_code == 0
    hist = PS.get(ws, "shop")["history"]
    assert [x["what"] for x in hist[n:]] == ["add a slot", "pin an input"] and all(x["applied_by"] == "claude" for x in hist[n:])
    assert run.invoke(app, ["--json", "--dry-run", "-w", "t", "projects", "slot-add", "shop", "Dry"]).exit_code == 0
    assert len(PS.get(ws, "shop")["history"]) == len(hist), "a dry run writes nothing"
    # A person is unaffected: no flag needed, no agent named, no history line for their own commands.
    monkeypatch.delenv("CLAUDECODE")
    assert cli("slot-add", "shop", "Sessions2").exit_code == 0
    assert len(PS.get(ws, "shop")["history"]) == len(hist)
    r = PA.accept(ws, "shop", p2["id"])
    assert r["proposal"]["status"] == "accepted" and "applied_by" not in r["proposal"]
    assert len(PS.get(ws, "shop")["history"]) == len(hist)
