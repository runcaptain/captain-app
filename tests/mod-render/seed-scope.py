#!/usr/bin/env python3
"""Shot check (SHOT_SCOPE=1): a brand with one dataset linked to it, so the window can start with the Explorer's pick
on that brand and show only that dataset. Prints the scope's id. Run with cst's own Python and CST_HOME."""
from captain_search import scopes as SC
from captain_search.workspace import open_context

ctx = open_context()
s = SC.add_scope(ctx, "brand", "Shot Brand")
SC.link(ctx, [{"kind": "dataset", "name": "demo-catalog"}], scope=s["id"])
print(s["id"])
