#!/usr/bin/env python3
"""Shot check: save one notebook in the scratch workspace (after `cst demo seed`): a text cell, a SQL cell on the
demo store, a Python cell that reads its result, and a bar chart of it. Run with cst's own Python and CST_HOME."""
from captain_search import notebook as NB
from captain_search.workspace import open_context


def cell(cid, kind, source, **meta):
    base = {"id": cid, "metadata": {"captain": {"kind": kind, **meta}}, "source": source}
    if kind == "markdown":
        return {**base, "cell_type": "markdown"}
    return {**base, "cell_type": "code", "outputs": [], "execution_count": None}


nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
      "cells": [
          cell("intro", "markdown", "# Price by category\n- the demo store's products, totalled in SQL"),
          cell("prices", "sql", "select category, count(*) as products, round(avg(price), 2) as avg_price\nfrom products\ngroup by 1\norder by 3 desc",
               conn="demo-store", var="df_1"),
          cell("check", "python", "print(f\"{len(df_1)} categories, priciest: {df_1.iloc[0]['category']}\")"),
          cell("chart", "chart", "df_1", chart={"mark": "bar", "data": "df_1", "x": "category", "y": "avg_price"}),
      ]}
ctx = open_context()
m = NB.save_revision(ctx, "shot-notebook", nb, "shot check")
print(f"{m.name}@v{m.version}")
# The same query with its chart first, so a shot shows the chart above the fold. Charts read any cell's result.
chart_first = {**nb, "cells": [nb["cells"][3], nb["cells"][1]]}
m = NB.save_revision(ctx, "shot-chart", chart_first, "shot check")
print(f"{m.name}@v{m.version}")
