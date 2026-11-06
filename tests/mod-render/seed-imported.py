#!/usr/bin/env python3
"""Shot check: add one imported run (mode "imported": a readout's intervals, no stored rankings) to the scratch
workspace, the way `cst demos import` does, so Comparisons can be shot on one. Run with cst's own Python and
CST_HOME pointing at the scratch workspace. Prints the backtest name."""
import json

from captain_search import ops
from captain_search.model.entities import ArmResult, Backtest, BacktestRun, Evidence, Preregistration
from captain_search.workspace import open_context, whoami

ctx = open_context()
name = "demos/shot/imported-readout"
a, b = "demo-bm25@v1", "demo-tuned@v1"
comps = [
    {"metric": "conversion.rel", "delta": 0.031, "ci_low": 0.004, "ci_high": 0.058, "verdict": "win", "a": a, "b": b,
     "evidence": "SIMULATED", "p": None, "mde": 0.0, "primary": True},
    {"metric": "segments.mobile.lift", "delta": -0.01, "ci_low": -0.03, "ci_high": 0.01, "verdict": "noise", "a": a, "b": b,
     "evidence": "SIMULATED", "p": None, "mde": 0.0, "primary": False},
]
entry = ctx.registry.put_blob(json.dumps({"comparisons": comps}).encode(), "readout/shot.json")
bt = ctx.registry.publish(Backtest(
    org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0, mode="imported",
    arms=[a, b], queries="(from the original readout)",
    preregistration=Preregistration(hypothesis="Imported readout for the shot check", primary_metric="conversion.rel", mde=0.0),
    description="imported from s3://example/readout.json"))
ctx.registry.publish(BacktestRun(
    org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0, backtest=ops.ref(bt),
    mode="imported", source="s3://example/readout.json", arms=[ArmResult(arm=a, evidence=Evidence.SIMULATED),
    ArmResult(arm=b, evidence=Evidence.SIMULATED, logs=[entry])], comparisons=comps, files=[entry], complete=True,
    independent=True, description="2 intervals from readout.json; simulated shoppers"))
print(name)
