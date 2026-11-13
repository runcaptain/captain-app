"""Tree rankers: a LightGBM model in LightGBM's own text format, read with plain Python.

These are trees, not neurons. A tree asks a row of yes or no questions about the inputs ("is input 7 at most
0.35?") and ends at a leaf, which holds one number. The model's answer is the sum of the leaves reached, one per
tree. There are no weights or biases to nudge, and no gradient to follow through them here.

Nothing is run. `parse` reads the file. `summary` says, for every input, how many questions are asked about it and
how much those questions gained in training, names the inputs no tree ever asks about, and gives the first few
trees as structures a screen can draw.

The text format, as LightGBM writes it: a header of `key=value` lines, then one block per tree starting `Tree=N`
with space-separated lists (`split_feature`, `threshold`, `split_gain`, `left_child`, `right_child`,
`leaf_value`, ...), then `end of trees`. A child that is 0 or more is another question. A child below 0 is a leaf:
leaf number `-child - 1`.
"""
from __future__ import annotations

import json

from .net import WeightsError

SAYS = ("These are trees, not neurons. Each tree asks yes or no questions about the inputs and ends at a leaf, and the "
        "answer is the sum of the leaves reached. There are no weights or biases to nudge.")
RULE = "At each question, a value at or below the threshold goes left. Anything else goes right."
TREES_SHOWN = 3
TREES_MOST = 20
NOT_THIS = "This is not a LightGBM model in its text format: {why}."


def _nums(text: str, kind=float) -> list:
    out = []
    for v in text.split():
        try:
            out.append(kind(v))
        except ValueError:
            out.append(kind(0))
    return out


def parse(text: str) -> dict:
    """A LightGBM text model as {header, names, infos, trees, parameters}. Refuses a file that is not one."""
    if not isinstance(text, str):
        try:
            text = bytes(text).decode()
        except (UnicodeDecodeError, TypeError, ValueError):
            raise WeightsError(NOT_THIS.format(why="it is not text")) from None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "tree":
        raise WeightsError(NOT_THIS.format(why="its first line should be the word tree"))
    header: dict[str, str] = {}
    trees: list[dict] = []
    params: dict[str, str] = {}
    cur: dict | None = None
    where = "header"
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line == "end of trees":
            where, cur = "after", None
            continue
        if line == "parameters:":
            where = "parameters"
            continue
        if line == "end of parameters":
            where = "after"
            continue
        if where == "parameters":
            if line.startswith("[") and line.endswith("]") and ":" in line:
                k, _, v = line[1:-1].partition(":")
                params[k.strip()] = v.strip()
            continue
        if where == "after":
            continue
        if line.startswith("Tree="):
            cur = {"tree": len(trees)}
            trees.append(cur)
            where = "trees"
            continue
        k, eq, v = line.partition("=")
        if not eq:
            continue
        (header if cur is None else cur)[k.strip()] = v.strip()
    if "max_feature_idx" not in header:
        raise WeightsError(NOT_THIS.format(why="it does not say how many inputs it has"))
    try:
        n_inputs = int(header["max_feature_idx"]) + 1
    except ValueError:
        raise WeightsError(NOT_THIS.format(why="its count of inputs is not a number")) from None
    names = header.get("feature_names", "").split()
    infos = header.get("feature_infos", "").split()
    out_trees = []
    for t in trees:
        try:
            leaves = int(t.get("num_leaves", "1"))
        except ValueError:
            raise WeightsError(NOT_THIS.format(why=f"tree {t['tree'] + 1} does not say how many leaves it has")) from None
        one = {"tree": t["tree"], "leaves": leaves, "shrinkage": _nums(t.get("shrinkage", "1"))[0] if t.get("shrinkage") else 1.0,
               "feature": _nums(t.get("split_feature", ""), int), "gain": _nums(t.get("split_gain", "")),
               "threshold": _nums(t.get("threshold", "")), "decision": _nums(t.get("decision_type", ""), int),
               "left": _nums(t.get("left_child", ""), int), "right": _nums(t.get("right_child", ""), int),
               "leaf_value": _nums(t.get("leaf_value", "")), "leaf_count": _nums(t.get("leaf_count", ""), int),
               "internal_count": _nums(t.get("internal_count", ""), int), "internal_value": _nums(t.get("internal_value", ""))}
        n = leaves - 1
        if any(len(one[k]) != n for k in ("feature", "threshold", "left", "right")) or len(one["leaf_value"]) != leaves:
            raise WeightsError(NOT_THIS.format(why=f"tree {t['tree'] + 1} has lists that do not match its {leaves} leaves"))
        if any(not 0 <= f < n_inputs for f in one["feature"]):
            raise WeightsError(NOT_THIS.format(why=f"tree {t['tree'] + 1} asks about an input the model does not have"))
        if any(not -leaves <= c < n for c in one["left"] + one["right"]):
            raise WeightsError(NOT_THIS.format(why=f"tree {t['tree'] + 1} points at a question or a leaf it does not have"))
        if len(one["gain"]) != n:
            one["gain"] = [0.0] * n
        if len(one["decision"]) != n:
            one["decision"] = [2] * n
        out_trees.append(one)
    return {"header": header, "inputs": n_inputs, "names": names if len(names) == n_inputs else [], "infos": infos if len(infos) == n_inputs else [],
            "objective": header.get("objective", "").split()[0] if header.get("objective") else "", "trees": out_trees, "parameters": params}


def names_from(cols, n_inputs: int) -> list[str]:
    """The inputs' names from a `*.cols.json` beside the model: a list of names, or an object that holds one."""
    if isinstance(cols, (bytes, bytearray, str)):
        try:
            cols = json.loads(cols)
        except (ValueError, UnicodeDecodeError):
            return []
    if isinstance(cols, dict):
        for v in cols.values():
            if isinstance(v, list) and len(v) == n_inputs and all(isinstance(x, str) for x in v):
                return v
        return []
    if isinstance(cols, list) and len(cols) == n_inputs and all(isinstance(x, str) for x in cols):
        return cols
    return []


def _depth(t: dict, node: int = 0) -> int:
    if t["leaves"] <= 1:
        return 0
    deepest, todo = 0, [(node, 1)]
    seen = 0
    while todo and seen <= 2 * t["leaves"]:
        at, d = todo.pop()
        seen += 1
        deepest = max(deepest, d)
        for child in (t["left"][at], t["right"][at]):
            if child >= 0:
                todo.append((child, d + 1))
    return deepest


def _draw(t: dict, names: list[str]) -> dict:
    """One tree as nested questions and leaves."""
    def leaf(i: int) -> dict:
        return {"leaf": i, "value": t["leaf_value"][i], **({"count": t["leaf_count"][i]} if i < len(t["leaf_count"]) else {})}

    if t["leaves"] <= 1:
        return leaf(0)
    budget = [2 * t["leaves"]]

    def node(i: int) -> dict:
        budget[0] -= 1
        if budget[0] < 0:
            raise WeightsError(NOT_THIS.format(why=f"tree {t['tree'] + 1} loops back on itself"))
        if i < 0:
            return leaf(-i - 1)
        f, d = t["feature"][i], t["decision"][i]
        return {"input": f, "name": names[f] if f < len(names) else f"input {f + 1}", "threshold": t["threshold"][i], "gain": t["gain"][i],
                "categorical": bool(d & 1), "missing_goes": "left" if d & 2 else "right",
                **({"count": t["internal_count"][i]} if i < len(t["internal_count"]) else {}),
                "left": node(t["left"][i]), "right": node(t["right"][i])}
    return node(0)


def summary(model: dict, names: list[str] | None = None, trees: int = TREES_SHOWN) -> dict:
    """What a parsed model asks about: per input its questions and their gain, the inputs never asked about, and
    the first `trees` trees to draw."""
    try:
        shown = max(0, min(int(trees), TREES_MOST))
    except (TypeError, ValueError):
        raise WeightsError("The number of trees to show is a whole number.") from None
    n = model["inputs"]
    names = list(names) if names and len(names) == n else (model["names"] or [f"input {i + 1}" for i in range(n)])
    splits, gain, used_in = [0] * n, [0.0] * n, [0] * n
    for t in model["trees"]:
        hit = set()
        for f, g in zip(t["feature"], t["gain"]):
            splits[f] += 1
            gain[f] += g if g == g else 0.0
            hit.add(f)
        for f in hit:
            used_in[f] += 1
    total_gain, total_splits = sum(gain), sum(splits)
    infos = model["infos"]
    rows = [{"input": i, "name": names[i], "splits": splits[i], "gain": gain[i],
             "share": (gain[i] / total_gain) if total_gain else 0.0, "trees": used_in[i]} for i in range(n)]
    unused = [{"input": i, "name": names[i], "constant": bool(infos) and infos[i] == "none"} for i in range(n) if not splits[i]]
    if unused:
        said = ", ".join(u["name"] for u in unused[:8]) + (f" and {len(unused) - 8} more" if len(unused) > 8 else "")
        unused_says = (f"{len(unused)} of {n} inputs are never asked about by any tree, so changing them cannot change the answer: {said}.")
    else:
        unused_says = f"Every one of the {n} inputs is asked about by at least one tree."
    return {"kind": "trees", "says": SAYS, "rule": RULE, "objective": model["objective"] or "not stated", "trees": len(model["trees"]),
            "inputs": n, "leaves": sum(t["leaves"] for t in model["trees"]), "splits": total_splits, "gain": total_gain,
            "unused": unused, "unused_says": unused_says,
            "by_input": sorted(rows, key=lambda r: (-r["gain"], -r["splits"], r["input"])),
            "first_trees": [{"tree": t["tree"], "leaves": t["leaves"], "depth": _depth(t), "shrinkage": t["shrinkage"], "root": _draw(t, names)}
                            for t in model["trees"][:shown]],
            "editable": False, "runnable": False}
