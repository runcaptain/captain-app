"""cst weights for a trained session model with rankers: its parts in words, and the commands that read it.

    cst weights import <folder or s3://...> --as <name>     (in weights.py: it hands the folder to real.py)
    cst weights nearest <tensor> <row>                      the rows of a table that point the same way as one row
    cst weights directions <tensor>                         a table's largest directions
    cst weights attention [--layer 1] [--head 1]            what each head looks for and what it copies
    cst weights trees <model> [--ranker full]               a tree ranker: its inputs, the unused ones, its first trees

Rows count from 0 because they are positions inside a tensor. --layer and --head count from 1, as everywhere a
person names a place. Everything in --json counts from 0.
"""
from __future__ import annotations

from typing import Optional

import typer

from ..weights import real as R
from .main import ctx, emit
from .weights import SESSION, _do, _f, _n, _one, _sid, weights_app


def _shape(shape) -> str:
    return " by ".join(str(s) for s in shape) if shape else "one number"


def _head_words(d: dict) -> str:
    return _n(d["heads"], "head") if d.get("heads") else "heads unknown"


def parts_text(d: dict) -> str:
    lines = [f"{d['kind_says']}: width {d['width']}, {_n(d['depth'], 'layer')}, {_head_words(d)}, {d['parameters']:,} numbers.",
             d["heads_says"]]
    for p in d["parts"]:
        size = f"{_shape(p['shape'])}, {p['parameters']:,} numbers" if p["shape"] else "one number"
        lines.append(f"  {p['name']} ({p['technical']}): {size}. {p['says']}")
        for mt in p.get("matrices") or []:
            lines.append(f"      {mt['name']}: rows {mt['rows'][0]}:{mt['rows'][1]} of {mt['tensor']}")
    lines.append(d["wiring_says"])
    if d.get("facts"):
        lines.append("kept as facts, not tensors: " + ", ".join(f["name"] for f in d["facts"]))
    for r in d.get("rankers") or []:
        lines.append(f"tree ranker {r['name']}: {_n(r['trees'], 'tree')}, {_n(r['inputs'], 'input')}, objective {r['objective']}. "
                     f"See it with `cst weights trees <model> --ranker {r['name']}`.")
    for t in d.get("tables") or []:
        lines.append(f"lookup table {t['name']}: {_n(len(t['arrays']), 'array')}. {t['says']}")
    if d.get("other_files"):
        lines.append(f"{_n(len(d['other_files']), 'other file')} kept unchanged, not read")
    lines.append(d["can"]["says"])
    return "\n".join(lines)


def show_text(w: dict) -> str:
    src = w.get("source") or {}
    return "\n".join([f"{w['name']} v{w['version']}. {w['says']}", parts_text(w),
                      f"{w['source_says']} It came from {src.get('from')}. Tensor digest {str(w.get('sha256'))[:16]}.",
                      f"To read its numbers, open a session: `cst weights open {w['name']}`."])


def import_text(out: dict) -> str:
    return (f"imported {out['ref']}: {out['says']} {out['parameters']:,} numbers in {_n(out['tensors'], 'tensor')}, "
            f"{_n(len(out['files']), 'file')} kept unchanged.\n"
            + (f"tree rankers: {', '.join(out['rankers'])}\n" if out["rankers"] else "")
            + (f"kept as facts, not tensors: {', '.join(out['facts'])}\n" if out["facts"] else "")
            + f"tensor digest: {out['sha256']}\n{R.NOT_YET}\nSee its parts with `cst weights show {out['model']}`.")


def labels_text(r: dict) -> str:
    if not r["labels"]:
        return f"{r['tensor']}: {r['rows']:,} rows, numbered. " + (r["labels_says"] or "No list of names for them was saved beside the arrays.")
    return "\n".join([f"{r['tensor']}: {r['rows']:,} rows. {r['labels_says']}"] + [f"  row {i}: {name}" for i, name in enumerate(r["labels"], r["rows_shown"][0])])


@weights_app.command("labels")
def labels(tensor: str = typer.Argument(..., help="a table by name, such as price.weight"),
           rows: Optional[str] = typer.Option(None, "--rows", help="only these rows, counted from 0: 3 or 0:50"), session: Optional[str] = SESSION):
    """What each row of a table stands for, when a list saved beside the arrays says: category names, price ranges."""
    r = _do(R.labels, ctx(), _sid(session), tensor, rows=rows)
    emit(r, labels_text(r))


def _row(r: dict) -> str:
    return f"row {r['row']}" + (f" ({r['label']})" if r.get("label") else "")


@weights_app.command("nearest")
def nearest(tensor: str = typer.Argument(..., help="a table by name, such as act.weight"),
            row: int = typer.Argument(..., help="the row to compare the others with, counted from 0"),
            top: int = typer.Option(10, "--top", help="how many rows to list"), session: Optional[str] = SESSION):
    """The rows of a table that point the same way as one row, by cosine. Nothing is run."""
    r = _do(R.nearest, ctx(), _sid(session), tensor, row, top)
    lines = [f"{r['tensor']}, {_row(r)} of {r['rows']:,}. Nearest:"]
    lines += [f"  {_row(n)}: cosine {n['cosine']:.3f}" for n in r["nearest"]]
    lines += ["Farthest:"] + [f"  {_row(n)}: cosine {n['cosine']:.3f}" for n in r["farthest"]]
    emit(r, "\n".join(lines + [r["says"]] + ([r["labels_says"]] if r["labels_says"] else [])))


@weights_app.command("directions")
def directions(tensor: str = typer.Argument(..., help="a table by name, such as pos.weight"),
               top: int = typer.Option(5, "--top", help="how many directions to describe"), session: Optional[str] = SESSION):
    """A table's largest directions: the patterns its rows share, largest first. Nothing is run."""
    r = _do(R.directions, ctx(), _sid(session), tensor, top)
    lines = [f"{r['tensor']}, {_shape(r['shape'])}: {r['needed_for_90']} of {r['of']} directions carry 90% of its size."]
    for d in r["directions"]:
        lines.append(f"  direction {d['direction'] + 1}: size {_f(d['size'])}, {d['share'] * 100:.1f}% of the table. Leaning on it most: "
                     + ", ".join(f"{_row(s)} {s['amount']:+.3g}" for s in d["strongest_rows"]))
    emit(r, "\n".join(lines + [r["says"]]))


@weights_app.command("attention")
def attention(layer: int = typer.Option(1, "--layer", help="the attention layer, counted from 1"),
              head: Optional[int] = typer.Option(None, "--head", help="one head, counted from 1. Left out: every head"),
              heads: Optional[int] = typer.Option(None, "--heads", help="how many heads the model has, when its files do not say"),
              matrix: Optional[str] = typer.Option(None, "--matrix", help="with --head and --json: also send query_key or output_value whole"),
              session: Optional[str] = SESSION):
    """What each head of an attention block looks for and what it copies, composed from its matrices. Nothing is run."""
    r = _do(R.attention, ctx(), _sid(session), _one(layer, "--layer"), _one(head, "--head"), heads, matrix)
    lines = [f"attention layer {r['layer'] + 1}: width {r['width']}, {_n(r['heads'], 'head')} of {r['head_width']} ({r['heads_from']})."]
    for h in r["of"]:
        lines.append(f"head {h['head'] + 1}: looks for (query_key) size {_f(h['query_key']['size'])}, largest {', '.join(_f(v) for v in h['query_key']['top'][:3])}. "
                     f"Copies (output_value) size {_f(h['output_value']['size'])}, largest {', '.join(_f(v) for v in h['output_value']['top'][:3])}.")
        lines += [f"    {t['name']}: as query {_f(t['query'])}, as key {_f(t['key'])}, copied {_f(t['value'])}" for t in h["reads"]]
    emit(r, "\n".join(lines + [r["says"], r["reads_says"]]))


def _tree_lines(node: dict, depth: int = 0, limit: int = 4) -> list[str]:
    pad = "    " + "  " * depth
    if "leaf" in node:
        return [f"{pad}leaf: {_f(node['value'])}"]
    if depth >= limit:
        return [f"{pad}more questions below (use --json for the whole tree)"]
    return ([f"{pad}is {node['name']} at most {_f(node['threshold'])}? (gain {_f(node['gain'])})", f"{pad}yes:"] + _tree_lines(node["left"], depth + 1, limit)
            + [f"{pad}no:"] + _tree_lines(node["right"], depth + 1, limit))


@weights_app.command("trees")
def trees(model: str = typer.Argument(..., help="a session model with rankers, or one version of it"),
          ranker: Optional[str] = typer.Option(None, "--ranker", help="which ranker, such as full. Left out: the first"),
          trees: int = typer.Option(3, "--trees", help="how many of the first trees to give, up to 20"),
          inputs: int = typer.Option(15, "--inputs", help="how many inputs to list, largest gain first")):
    """A tree ranker: how much each input is used, the inputs no tree asks about, and its first trees. These are
    trees, not neurons: there are no weights or biases to nudge."""
    r = _do(R.trees, ctx(), model, ranker, trees)
    lines = [f"{r['model']} v{r['version']}, ranker {r['ranker']} (of {', '.join(r['rankers'])}): {_n(r['trees'], 'tree')}, {_n(r['inputs'], 'input')}, "
             f"{r['splits']:,} questions, objective {r['objective']}.", r["says"], "", "NOT USED: " + r["unused_says"], "",
             "inputs by how much their questions gained:"]
    lines += [f"  {i['name']}: {_n(i['splits'], 'question')}, {i['share'] * 100:.1f}% of the gain" for i in r["by_input"][:max(0, inputs)] if i["splits"]]
    lines += ["", "inputs by the start of their names:"]
    lines += [f"  {g['prefix']}: {_n(g['count'], 'input')}, {g['unused']} not used, {g['share'] * 100:.1f}% of the gain" for g in r["groups"]]
    for t in r["first_trees"]:
        lines += ["", f"tree {t['tree'] + 1}: {_n(t['leaves'], 'leaf', 'leaves')}, {t['depth']} deep"] + _tree_lines(t["root"])
    emit(r, "\n".join(lines + ["", r["rule"]]))


@weights_app.command("rankers")
def rankers(model: str = typer.Argument(..., help="a session model with rankers, or one version of it")):
    """The model's tree rankers side by side: which inputs each one asks about, and which none of them does."""
    r = _do(R.rankers, ctx(), model)
    lines = [f"{r['model']} v{r['version']}: " + ", ".join(f"{k['name']} uses {k['used']} of {k['inputs']} inputs" for k in r["rankers"]) + ".",
             "", "NOT USED BY ANY: " + r["unused_by_all_says"], "",
             f"used by all: {len(r['used_by_all'])}. Used by some and not others: {len(r['used_by_some'])}."]
    lines += [f"  {i['name']}: used by {', '.join(i['used_by'])}, not by {', '.join(i['unused_by'])}" for i in r["inputs"] if i["used_by"] and i["unused_by"]]
    emit(r, "\n".join(lines + ["", r["says"]]))


@weights_app.command("registry")
def registry(where: str = typer.Argument(..., help="a folder, or an s3:// address, that holds version folders")):
    """The trained versions under a folder or an address that can be brought in. Read only: nothing is written there."""
    r = _do(R.registry, where)
    lines = [f"  {v['address']}" + (f"\n      {v['description']}" if v["description"] else "") for v in r["versions"]]
    emit(r, "\n".join([r["says"]] + lines + ([f"  and {r['count'] - r['listed']} more"] if r["count"] > r["listed"] else [])))
