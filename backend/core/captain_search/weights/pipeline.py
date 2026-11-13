"""Bring your own training: a training command writes a network and its cost per step, and Captain picks both up.

The command (see docs/trainer-contract.md, "Writing a network") may write into its output folder:
  <CST_OUT>/<network_dir>/manifest.json + tensors   a model folder (files.py). It becomes a weights model version.
  <CST_OUT>/cost.jsonl                              one JSON object per line as training goes: {"step": 12, "cost": 0.31}

While the command runs, `CostTail` reads new lines of cost.jsonl and hands each to the live run, so the cost is on
the live feed as it falls. When the command ends, `collect` brings the model folder in with the cost history.
"""
from __future__ import annotations

import json
import os
import threading

from . import store as WS
from .net import WeightsError

COST_FILE = "cost.jsonl"
KEEP = 2000    # how many cost points are kept with the model


def parse_cost(line: str) -> tuple[float | None, float] | None:
    """One line of a cost file as (step, cost), or None for a line that is not one. `loss` is read as cost."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        d = json.loads(line)
        value = d.get("cost", d.get("loss"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        step = d.get("step")
        return (float(step) if isinstance(step, (int, float)) and not isinstance(step, bool) else None), float(value)
    except (ValueError, AttributeError):
        return None


def read_costs(path: str) -> list[list[float]]:
    """Every cost point in a cost file as [step, cost], thinned evenly to at most KEEP points."""
    try:
        with open(path) as f:
            rows = [p for p in (parse_cost(ln) for ln in f) if p is not None]
    except OSError:
        return []
    pts = [[s if s is not None else float(i + 1), c] for i, (s, c) in enumerate(rows)]
    if len(pts) > KEEP:
        stride = len(pts) / KEEP
        pts = [pts[int(i * stride)] for i in range(KEEP - 1)] + [pts[-1]]
    return pts


class CostTail:
    """Follows a cost file while a training command writes it. Each new whole line goes to `on_cost(step, cost)`."""

    EVERY_S = 0.2

    def __init__(self, path: str, on_cost):
        self.path, self.on_cost = path, on_cost
        self.at, self.rest, self.n = 0, "", 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="weights-cost-tail")

    def start(self) -> "CostTail":
        self.thread.start()
        return self

    def look(self) -> None:
        try:
            with open(self.path) as f:
                f.seek(self.at)
                text = f.read()
                self.at = f.tell()
        except OSError:
            return
        lines = (self.rest + text).split("\n")
        self.rest = lines.pop()    # a line still being written waits for its end
        for ln in lines:
            p = parse_cost(ln)
            if p is not None:
                self.n += 1
                try:
                    self.on_cost(p[0] if p[0] is not None else float(self.n), p[1])
                except Exception:  # noqa: BLE001  a feed that cannot be told must not stop the training
                    pass

    def _run(self) -> None:
        while not self._stop.wait(self.EVERY_S):
            self.look()

    def stop(self) -> int:
        """Stop following, after one last look. Returns how many cost points were passed on."""
        self._stop.set()
        self.thread.join(2)
        self.look()
        return self.n


def follow(out_dir: str, live) -> CostTail:
    """Stream <out_dir>/cost.jsonl to a live run as the metric `cost`."""
    return CostTail(os.path.join(out_dir, COST_FILE), lambda step, cost: live.metric("cost", cost, step=step)).start()


def collect(ctx, out_dir: str, network_dir: str, name: str, trained_by: dict | None = None) -> dict:
    """Bring the model folder a training command wrote in as the next version of the weights model `name`."""
    base = os.path.realpath(network_dir if os.path.isabs(network_dir) else os.path.join(out_dir, network_dir))
    if not os.path.isdir(base):
        raise WeightsError(f"network_dir {network_dir!r} is not a folder. Write the model's manifest.json and tensors into it.")
    costs = read_costs(os.path.join(out_dir, COST_FILE)) or read_costs(os.path.join(base, COST_FILE))
    extra: dict = {}
    if trained_by or costs:
        extra["training"] = {**(trained_by or {}), **({"cost": costs, "cost_first": costs[0][1], "cost_last": costs[-1][1],
                                                        "steps": len(costs)} if costs else {})}
    m = WS.import_folder(ctx, base, name, **extra)
    return {"model": m["name"], "version": m["version"], "ref": WS.ref_of(m["name"], m["version"]), "parameters": m["parameters"],
            "sizes": m["sizes"], "cost_points": len(costs)}
