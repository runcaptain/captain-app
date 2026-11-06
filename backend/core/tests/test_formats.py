import ast
import json

import pytest

from captain_search.notebook import new_notebook, normalize
from captain_search.notebook.formats import FORMATS, MARIMO_ORDER_WARNING, detect, from_ipynb, to_ipynb


def sig(nb):
    """What must survive a round trip: cell kind, source, and the SQL connection / result name."""
    out = []
    for c in nb["cells"]:
        meta = c["metadata"].get("captain") or {}
        if c["cell_type"] == "markdown":
            kind = "markdown"
        elif c["cell_type"] == "raw":
            kind = "raw"
        else:
            kind = meta.get("kind") or "python"
        out.append((kind, c["source"].strip(), meta.get("conn"), meta.get("var")))
    return out


def roundtrip(filename, text):
    nb1, fmt, warnings = to_ipynb(filename, text)
    normalize(json.loads(json.dumps(nb1)))
    ext, out, mime = from_ipynb(nb1, fmt)
    nb2, fmt2, _ = to_ipynb("again" + ext, out)
    assert fmt2 == fmt
    assert sig(nb2) == sig(nb1)
    ext3, out3, _ = from_ipynb(nb2, fmt)
    assert out3 == out or fmt in ("zeppelin",)  # zeppelin ids are random
    return nb1, warnings, out


JUPYTEXT = '''# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Zero results
#
# Queries with no hits.

# %%
import pandas as pd
x = 1

# %% [sql] conn=demo-store var=zero
SELECT query, count(*) AS n FROM searches WHERE hits = 0 GROUP BY 1

# %%
%%sql conn=warehouse
SELECT 1
'''

DATABRICKS = '''# Databricks notebook source
# MAGIC %md
# MAGIC # Zero results
# MAGIC
# MAGIC Queries with no hits.

# COMMAND ----------

import pandas as pd
x = 1

# COMMAND ----------

# MAGIC %sql
# MAGIC -- captain: conn=demo-store var=zero
# MAGIC SELECT query, count(*) AS n
# MAGIC FROM searches WHERE hits = 0 GROUP BY 1

# COMMAND ----------

# MAGIC %python
# MAGIC print(x)

# COMMAND ----------

# MAGIC %scala
# MAGIC val y = 2
'''

MARIMO = '''import marimo

__generated_with = "0.9.14"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Zero results

    Queries with no hits.
    """)
    return


@app.cell
def _():
    import pandas as pd
    x = 1
    return pd, x


@app.cell
def _(mo):
    zero = mo.sql(
        f"""
        SELECT query, count(*) AS n FROM searches WHERE hits = 0 GROUP BY 1
        """
    )
    return (zero,)


@app.cell
def _(x, zero):
    print(x, len(zero))
    return


if __name__ == "__main__":
    app.run()
'''

QUARTO = '''---
title: Zero results
format: html
---

# Zero results

Queries with no hits.

```{python}
#| echo: false
import pandas as pd
x = 1
```

```{sql}
#| connection: demo-store
#| output.var: zero
SELECT query, count(*) AS n FROM searches WHERE hits = 0 GROUP BY 1
```

Some text between.

```{r}
summary(cars)
```
'''

RMARKDOWN = '''---
title: "Zero results"
output: html_document
---

# Zero results

```{python}
x = 1
```

```{sql, connection="demo-store", output.var="zero"}
SELECT query FROM searches WHERE hits = 0
```

```{r setup, include=FALSE}
library(DBI)
```
'''

MYST = '''---
jupytext:
  text_representation:
    extension: .md
    format_name: myst
kernelspec:
  display_name: Python 3
  language: python
  name: python3
---

# Zero results

An example that is not executed:

```python
print("not a cell")
```

```{code-cell} ipython3
import pandas as pd
x = 1
```

```{code-cell} sql
:conn: demo-store
:var: zero
SELECT query FROM searches WHERE hits = 0
```

+++

Second markdown cell.

```{code-cell} r
summary(cars)
```
'''

ZEPPELIN = json.dumps({
    "name": "Zero results",
    "paragraphs": [
        {"text": "%md\n# Zero results\n\nQueries with no hits."},
        {"text": "%pyspark\nx = 1"},
        {"text": "%jdbc(demo-store)\nSELECT query FROM searches WHERE hits = 0", "config": {"captain": {"var": "zero"}}},
        {"text": "%spark.sql\nSELECT 1"},
        {"text": "%sh\nls -la"},
        {"text": ""},
    ],
})

FIXTURES = {
    "jupytext": ("analysis.py", JUPYTEXT),
    "databricks": ("analysis.py", DATABRICKS),
    "marimo": ("analysis.py", MARIMO),
    "quarto": ("analysis.qmd", QUARTO),
    "rmarkdown": ("analysis.Rmd", RMARKDOWN),
    "myst": ("analysis.md", MYST),
    "zeppelin": ("analysis.zpln", ZEPPELIN),
}


def test_formats_listing():
    ids = [f["id"] for f in FORMATS]
    assert ids == ["ipynb", "jupytext", "databricks", "marimo", "quarto", "rmarkdown", "myst", "zeppelin"]
    for f in FORMATS:
        assert f["import"] and f["export"] and f["extensions"]


def test_detect_python_flavours():
    assert detect("a.py", DATABRICKS) == "databricks"
    assert detect("a.py", MARIMO) == "marimo"
    assert detect("a.py", "app = marimo.App()\n") == "marimo"
    assert detect("a.py", JUPYTEXT) == "jupytext"
    assert detect("a.py", "x = 1\n") == "jupytext"


def test_detect_by_extension_and_content():
    assert detect("a.ipynb", "{}") == "ipynb"
    assert detect("a.qmd", "") == "quarto"
    assert detect("a.Rmd", "") == "rmarkdown"
    assert detect("a.md", "") == "myst"
    assert detect("a.zpln", "") == "zeppelin"
    assert detect("upload", json.dumps(new_notebook())) == "ipynb"
    assert detect("upload", ZEPPELIN) == "zeppelin"
    with pytest.raises(ValueError):
        detect("notes.txt", "hello")


@pytest.mark.parametrize("fmt", list(FIXTURES))
def test_fixture_roundtrip(fmt):
    filename, text = FIXTURES[fmt]
    nb, warnings, out = roundtrip(filename, text)
    assert detect(filename, text) == fmt
    kinds = [k for k, *_ in sig(nb)]
    assert "markdown" in kinds and "python" in kinds and "sql" in kinds
    for c in nb["cells"]:
        assert isinstance(c["source"], str)


def test_jupytext_cells():
    nb, warnings, out = roundtrip(*FIXTURES["jupytext"])
    assert sig(nb) == [
        ("markdown", "# Zero results\n\nQueries with no hits.", None, None),
        ("python", "import pandas as pd\nx = 1", None, None),
        ("sql", "SELECT query, count(*) AS n FROM searches WHERE hits = 0 GROUP BY 1", "demo-store", "zero"),
        ("sql", "SELECT 1", "warehouse", None),
    ]
    assert warnings == []
    assert "format_name: percent" in out and "name: python3" in out
    assert "# %% [sql] conn=demo-store var=zero" in out


def test_databricks_cells_and_scala_warning():
    nb, warnings, out = roundtrip(*FIXTURES["databricks"])
    s = sig(nb)
    assert [k for k, *_ in s] == ["markdown", "python", "sql", "python", "raw"]
    assert s[0][1] == "# Zero results\n\nQueries with no hits."
    assert s[2] == ("sql", "SELECT query, count(*) AS n\nFROM searches WHERE hits = 0 GROUP BY 1", "demo-store", "zero")
    assert s[3][1] == "print(x)"
    assert nb["cells"][4]["metadata"]["captain"]["lang"] == "scala"
    assert len(warnings) == 1 and "%scala" in warnings[0]
    assert out.startswith("# Databricks notebook source\n")
    assert "# MAGIC %scala\n# MAGIC val y = 2" in out


def test_marimo_cells_and_order_warning():
    nb, warnings, out = roundtrip(*FIXTURES["marimo"])
    assert sig(nb) == [
        ("markdown", "# Zero results\n\nQueries with no hits.", None, None),
        ("python", "import pandas as pd\nx = 1", None, None),
        ("sql", "SELECT query, count(*) AS n FROM searches WHERE hits = 0 GROUP BY 1", None, "zero"),
        ("python", "print(x, len(zero))", None, None),
    ]
    assert warnings == [MARIMO_ORDER_WARNING]
    tree = ast.parse(out)  # the export is valid Python
    assert "def _(x, zero):" in out and "return (pd, x)" in out and "return (zero,)" in out
    assert any(isinstance(n, ast.If) for n in tree.body)


def test_marimo_sql_conn_survives():
    nb = new_notebook()
    nb["cells"] = [{"cell_type": "code", "source": "SELECT 1", "metadata": {"captain": {"kind": "sql", "conn": "demo-store", "var": "one"}}}]
    ext, out, _ = from_ipynb(nb, "marimo")
    back, fmt, _ = to_ipynb("x" + ext, out)
    assert fmt == "marimo"
    assert sig(back) == [("sql", "SELECT 1", "demo-store", "one")]


def test_quarto_front_matter_options_and_r_warning():
    nb, warnings, out = roundtrip(*FIXTURES["quarto"])
    s = sig(nb)
    assert [k for k, *_ in s] == ["markdown", "python", "sql", "markdown", "raw"]
    assert s[1][1] == "#| echo: false\nimport pandas as pd\nx = 1"
    assert s[2] == ("sql", "SELECT query, count(*) AS n FROM searches WHERE hits = 0 GROUP BY 1", "demo-store", "zero")
    assert nb["metadata"]["captain"]["front_matter"] == {"title": "Zero results", "format": "html"}
    assert len(warnings) == 1 and "{r}" in warnings[0]
    assert out.startswith("---\ntitle: Zero results\n")
    assert "```{r}\nsummary(cars)\n```" in out


def test_rmarkdown_header_options():
    nb, warnings, out = roundtrip(*FIXTURES["rmarkdown"])
    s = sig(nb)
    assert [k for k, *_ in s] == ["markdown", "python", "sql", "raw"]
    assert s[2] == ("sql", "SELECT query FROM searches WHERE hits = 0", "demo-store", "zero")
    assert nb["cells"][3]["metadata"]["captain"]["header"] == "r setup, include=FALSE"
    assert len(warnings) == 1
    assert '```{sql, connection="demo-store", output.var="zero"}' in out
    assert "```{r setup, include=FALSE}" in out


def test_myst_plain_fences_stay_markdown():
    nb, warnings, out = roundtrip(*FIXTURES["myst"])
    s = sig(nb)
    assert [k for k, *_ in s] == ["markdown", "python", "sql", "markdown", "raw"]
    assert '```python\nprint("not a cell")\n```' in s[0][1]
    assert s[2] == ("sql", "SELECT query FROM searches WHERE hits = 0", "demo-store", "zero")
    assert nb["metadata"]["kernelspec"]["name"] == "python3"
    assert "front_matter" not in nb["metadata"]["captain"]
    assert len(warnings) == 1 and "r code-cell" in warnings[0]


def test_zeppelin_interpreters():
    nb, warnings, out = roundtrip(*FIXTURES["zeppelin"])
    assert sig(nb) == [
        ("markdown", "# Zero results\n\nQueries with no hits.", None, None),
        ("python", "x = 1", None, None),
        ("sql", "SELECT query FROM searches WHERE hits = 0", "demo-store", "zero"),
        ("sql", "SELECT 1", None, None),
        ("raw", "%sh\nls -la", None, None),
    ]
    assert len(warnings) == 1 and "%sh" in warnings[0]
    note = json.loads(out)
    assert note["name"] == "Zero results"
    assert note["paragraphs"][2]["text"].startswith("%jdbc(demo-store)\n")


def test_ipynb_roundtrip_keeps_everything():
    nb = new_notebook()
    nb["cells"] = [
        {"cell_type": "markdown", "source": ["# Title\n", "text"], "metadata": {}},
        {"cell_type": "code", "source": "x = 1", "metadata": {}, "outputs": [], "execution_count": None},
    ]
    ext, out, mime = from_ipynb(nb, "ipynb")
    assert (ext, mime) == (".ipynb", "application/x-ipynb+json")
    back, fmt, warnings = to_ipynb("n.ipynb", out)
    assert fmt == "ipynb" and warnings == []
    assert sig(back) == [("markdown", "# Title\ntext", None, None), ("python", "x = 1", None, None)]


CANON = [
    ("markdown", "# Report\n\nSome *text*.", None, None),
    ("python", "import pandas as pd\nx = 1\n\ndef f(a):\n    return a + x", None, None),
    ("sql", "SELECT a, b\nFROM t\nWHERE a > 1", "demo-store", "zero"),
    ("python", "print(f(2), len(zero))", None, None),
]


def canonical_notebook():
    nb = new_notebook()
    for kind, src, conn, var in CANON:
        if kind == "markdown":
            nb["cells"].append({"cell_type": "markdown", "source": src, "metadata": {}})
        else:
            meta = {"captain": {"kind": "sql", "conn": conn, "var": var}} if kind == "sql" else {}
            nb["cells"].append({"cell_type": "code", "source": src, "metadata": meta})
    return normalize(nb)


@pytest.mark.parametrize("fmt", [f["id"] for f in FORMATS])
def test_ipynb_export_import_every_format(fmt):
    ext, out, mime = from_ipynb(canonical_notebook(), fmt)
    assert ext == next(f["extensions"][0] for f in FORMATS if f["id"] == fmt)
    back, got, _ = to_ipynb("export" + ext, out)
    assert got == fmt
    assert sig(back) == CANON


def test_unknown_export_format():
    with pytest.raises(ValueError):
        from_ipynb(new_notebook(), "docx")
