"""cst notebook show and cell add, edit and rm: work on a notebook one cell at a time from the terminal."""
import json

from typer.testing import CliRunner

from captain_search.cli.main import app
from captain_search.workspace import init_workspace


def test_cells_from_the_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    init_workspace("t", str(tmp_path / "b"))
    r = CliRunner()

    def cst(*a):
        out = r.invoke(app, ["--json", *a])
        assert out.exit_code == 0, out.output
        return json.loads(out.output) if out.output.strip().startswith("{") else out.output

    cst("notebook", "new", "nb")
    cst("notebook", "cell", "add", "nb", "--kind", "markdown", "--source", "# Notes", "--at", "1")
    cst("notebook", "cell", "add", "nb", "--source", "x = 2")
    cst("notebook", "cell", "edit", "nb", "2", "--source", "select 2 as two", "--conn", "wh", "--var", "two")
    shown = cst("notebook", "show", "nb")
    kinds = [((c.get("metadata") or {}).get("captain") or {}).get("kind") or c["cell_type"] for c in shown["cells"]]
    assert kinds == ["markdown", "sql", "python"] and shown["draft"]
    assert shown["cells"][1]["source"] == "select 2 as two" and shown["cells"][1]["metadata"]["captain"]["conn"] == "wh"
    cst("notebook", "cell", "rm", "nb", "1")
    assert len(cst("notebook", "show", "nb")["cells"]) == 2
    bad = r.invoke(app, ["notebook", "cell", "rm", "nb", "9"])
    assert bad.exit_code != 0 and "no cell 9" in bad.output
