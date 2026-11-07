"""Plan 0: extensions from the team's captain/ folder, validated, used by runs, and recorded in run manifests."""
import json
import subprocess

from typer.testing import CliRunner

from captain_search import extensions as X
from captain_search.cli.main import app
from captain_search.eval import metrics as M


def _project(tmp_path, extra: dict | None = None):
    path = X.scaffold(str(tmp_path / "repo"), "acme")
    for rel, text in (extra or {}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    return path


def test_scaffold_loads_and_validates(tmp_path, monkeypatch):
    path = _project(tmp_path)
    monkeypatch.setenv("CST_PROJECT", str(path))
    reg = X.load(fresh=True)
    assert reg.errors == []
    m = reg.get("metric", "relevant@5")
    assert m and m.source == "project:metrics/example_metric.py" and m.meta["higher_is_better"] is True
    assert reg.get("judge", "title-match").obj("red shoes", {"title": "Red shoes"})[0] == 3
    assert reg.project["path"] == str(path) and len(reg.project["sha256"]) == 64


def test_bad_extensions_are_reported_not_loaded(tmp_path, monkeypatch):
    path = _project(tmp_path, {
        "metrics/bad.py": "from captain_search.extensions import metric\n@metric('broken')\ndef f(ranked, grades):\n    return 'high'\n",
        "judges/crash.py": "raise RuntimeError('boom')\n",
        "metrics/empty.py": "x = 1\n",
    })
    monkeypatch.setenv("CST_PROJECT", str(path))
    reg = X.load(fresh=True)
    errs = {e["name"]: e["error"] for e in reg.errors}
    assert "returns a number" in errs["broken"] and "boom" in errs["crash"] and "registers nothing" in errs["empty"]
    assert reg.get("metric", "broken") is None and reg.get("metric", "relevant@5") is not None


def test_custom_metrics_flow_into_per_query_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_PROJECT", str(_project(tmp_path)))
    ex = X.load(fresh=True).custom("metric")
    pq = M.per_query({"q1": ["a", "b"], "q2": ["c"]}, {"q1": {"a": 3}, "q2": {"c": 0}}, ex)
    assert pq["q1"]["relevant@5"] == 0.2 and pq["q2"]["relevant@5"] == 0.0
    assert M.summarize(pq)["relevant@5"] == 0.1


def test_fingerprint_names_the_commit(tmp_path, monkeypatch):
    path = _project(tmp_path)
    repo = path.parent
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "."); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    fp = X.fingerprint(path)
    assert fp["commit"] and fp["dirty"] is False and fp["repo_path"] == "captain"
    (path / "metrics" / "example_metric.py").write_text((path / "metrics" / "example_metric.py").read_text() + "\n# edit\n")
    assert X.fingerprint(path)["dirty"] is True


def test_cli_check_fails_on_errors(tmp_path):
    good = _project(tmp_path)
    r = CliRunner().invoke(app, ["--json", "ext", "check", str(good)])
    assert r.exit_code == 0 and json.loads(r.stdout)["ok"] is True
    bad = _project(tmp_path / "b", {"judges/bad.py": "from captain_search.extensions import judge\n@judge('nine')\ndef j(q, p):\n    return 9\n"})
    r = CliRunner().invoke(app, ["--json", "ext", "check", str(bad)])
    assert r.exit_code == 1 and "0 to 3" in r.stdout


def test_backtest_uses_project_metric_and_records_manifest(tmp_path, monkeypatch):
    from captain_search import demo, ops
    from captain_search.workspace import init_workspace

    path = _project(tmp_path)
    monkeypatch.setenv("CST_PROJECT", str(path))
    X.load(fresh=True)
    c = init_workspace("t", str(tmp_path / "bucket"))
    demo.seed(c, progress=lambda s: None)
    run = ops.backtest_run(c, "demo-tuned-vs-bm25")
    assert all("relevant@5" in a.offline for a in run.arms)
    m = run.manifest
    assert m["project"]["path"] == str(path) and m["python"] and m["cst"]["version"]
    assert {"kind": "metric", "name": "relevant@5"}.items() <= next(e for e in m["extensions"] if e["name"] == "relevant@5").items()
    kinds = {(e["kind"], e["name"]): e["source"] for e in m["extensions"]}
    assert kinds[("simulator", "cascade-v1")] == "builtin" and ("engine", "local-bm25") in kinds


def test_store_demos_come_only_from_a_private_file(tmp_path, monkeypatch):
    import pytest

    from captain_search import brands

    monkeypatch.setenv("CST_DEMOS", str(tmp_path / "none.yaml"))
    with pytest.raises(FileNotFoundError, match="no demos file"):
        brands.load_demos()
    (tmp_path / "d.yaml").write_text("registry: s3://example-registry\nbrands:\n  acme: {label: Acme}\n")
    monkeypatch.setenv("CST_DEMOS", str(tmp_path / "d.yaml"))
    assert brands.load_demos() == ("s3://example-registry", {"acme": {"label": "Acme"}})


def test_project_folder_can_add_a_compute_provider(tmp_path, monkeypatch):
    from captain_search import compute as C
    from captain_search import compute_providers as P

    path = _project(tmp_path, {"compute/slurm.py": '''
from captain_search.compute_providers import ComputeProvider, status
from captain_search.extensions import compute


@compute("slurm", description="Our on-prem Slurm cluster")
class Slurm(ComputeProvider):
    provider, label = "none", "Slurm cluster"
    fields = [{"name": "partition", "label": "Partition", "required": True}]

    def plan(self, job):
        return [{"service": "slurm", "call": "sbatch", "args": {"partition": self.config["partition"], "argv": job.argv}}]

    def submit(self, job):
        return {"id": "123"}

    def status(self, handle):
        return status("succeeded", "finished")

    def logs(self, handle, since=None):
        return {"lines": [], "next": None}

    def cancel(self, handle):
        return status("stopped", "cancelled")

    def test(self):
        return {"ok": True, "detail": "reachable"}
''', "compute/broken.py": "from captain_search.extensions import compute\n@compute('nope')\nclass N:\n    pass\n"})
    monkeypatch.setenv("CST_PROJECT", str(path))
    reg = X.load(fresh=True)
    assert reg.get("compute", "slurm").source == "project:compute/slurm.py"
    assert any(e["name"] == "nope" and "ComputeProvider subclass" in e["error"] for e in reg.errors)
    kinds = C.kind_map()
    assert kinds["slurm"]["label"] == "Slurm cluster" and "ecs" in " ".join(kinds)
    prov = P.get({"kind": "slurm", "config": {"partition": "gpu"}})
    assert prov.test()["ok"] and prov.plan(P.Job(id="j", argv=["x"], label="l", env={}))[0]["args"]["partition"] == "gpu"
    assert "nope" in P.plugin_errors()
