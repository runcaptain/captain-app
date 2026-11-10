"""The starter trainer: LightGBM lambdarank over a Captain training set, through the trainer contract.

It is an ordinary trainer. It reads CST_TRAIN_INPUTS, CST_PARAMS and CST_OUT, and writes CST_OUT/result.yaml
(`cst docs trainer-contract`). Captain runs it in a private environment that holds LightGBM, so this file
imports nothing from Captain and can be read as an example of the contract.

In:  the input with role `training`: a dataset from `cst data training-set`. Its parquet part has query_id,
     grade, split (train or valid, by query) and one column per feature. Its features.json names the features and
     carries the lookup tables scoring needs.
Out: model/model.txt     LightGBM's own model file
     model/model.json    the same trees as JSON, which rank.py reads
     model/features.json the feature spec and tables, copied from the training set unchanged
     model/rank.py       a scorer that needs only numpy
     result.yaml         metrics (nDCG@10 on the validation queries), params, the artifact, and the spec keys that
                         tell the rerank model version which files to load

Training stops early when the validation nDCG@10 hasn't improved for `early_stopping_rounds` rounds. Each round
prints one JSON line, which Captain turns into the live chart under Runs.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import sys

DEFAULTS = {"rounds": 300, "early_stopping_rounds": 30, "learning_rate": 0.05, "num_leaves": 15,
            "min_data_in_leaf": 5, "seed": 7, "top_k": 50}


def say(**event) -> None:
    print(json.dumps(event), flush=True)


def fail(sentence: str) -> None:
    print(sentence, flush=True)
    sys.exit(2)


def read_bytes(uri: str) -> bytes:
    if "://" not in uri:
        return pathlib.Path(uri).read_bytes()
    from pyarrow import fs

    filesystem, path = fs.FileSystem.from_uri(uri)
    with filesystem.open_input_stream(path) as f:
        return f.read()


def main() -> None:
    try:
        import lightgbm as lgb
        import numpy as np
        import pyarrow.parquet as pq
    except ImportError as e:  # the launcher sets the environment up; this is for someone running the file by hand
        fail(f"The starter trainer needs LightGBM, numpy and pyarrow ({e}). Install them with "
             "`pip install 'captain-search[train-ltr]'`.")

    out = pathlib.Path(os.environ["CST_OUT"])
    inputs = json.loads(os.environ.get("CST_TRAIN_INPUTS") or "[]")
    params = {**DEFAULTS, **json.loads(os.environ.get("CST_PARAMS") or "{}")}
    ts = next((i for i in inputs if i.get("role") == "training"), None) or next((i for i in inputs if i.get("kind") == "training"), None)
    if ts is None:
        fail("The starter trainer reads a training set (an input with role `training`, built by `cst data "
             "training-set`), and none was given.")
    files = {f.get("path"): f["uri"] for f in ts.get("files") or []}
    if "features.json" not in files or not any(str(p).endswith(".parquet") for p in files if p):
        fail(f"{ts.get('ref')} isn't a training set from `cst data training-set`: it has no features.json.")
    bundle_bytes = read_bytes(files["features.json"])
    bundle = json.loads(bundle_bytes)
    names = bundle["names"]
    table = pq.read_table(io.BytesIO(read_bytes(next(u for p, u in sorted(files.items()) if p and p.endswith(".parquet")))))
    cols = {c: table.column(c).to_pylist() for c in ["query_id", "grade", "split"] + names}
    print(f"training on {ts.get('ref')}: {table.num_rows:,} rows, {len(names)} features, "
          f"{bundle.get('label_source') or 'unknown'} labels, nothing read after {ts.get('as_of') or 'no cutoff'}", flush=True)
    if bundle.get("simulated"):
        print("These labels come from simulated events. The model learns the simulator, not real shoppers.", flush=True)

    def part(which: str):
        idx = [i for i, s in enumerate(cols["split"]) if s == which]
        idx.sort(key=lambda i: cols["query_id"][i])  # stable: rows of one query stay together, in file order
        groups, last = [], None
        for i in idx:
            if cols["query_id"][i] != last:
                groups.append(0)
                last = cols["query_id"][i]
            groups[-1] += 1
        x = np.array([[cols[n][i] for n in names] for i in idx], dtype=float)
        y = np.array([cols["grade"][i] for i in idx], dtype=int)
        return x, y, groups

    xt, yt, gt = part("train")
    xv, yv, gv = part("valid")
    if not gt or not gv:
        fail("The training set has no train rows or no validation rows, so there is nothing to stop early on.")
    rounds, patience = int(params["rounds"]), int(params["early_stopping_rounds"])
    lgb_params = {"objective": "lambdarank", "metric": "ndcg", "eval_at": [10], "learning_rate": float(params["learning_rate"]),
                  "num_leaves": int(params["num_leaves"]), "min_data_in_leaf": int(params["min_data_in_leaf"]),
                  "seed": int(params["seed"]), "deterministic": True, "force_row_wise": True, "num_threads": 1,
                  "use_missing": False, "verbosity": -1}
    train = lgb.Dataset(xt, label=yt, group=gt, feature_name=names)
    valid = lgb.Dataset(xv, label=yv, group=gv, reference=train)
    history: dict = {}

    def each_round(env):
        scores = {name: value for name, metric, value, _ in env.evaluation_result_list if metric == "ndcg@10"}
        n = env.iteration + 1
        for name, value in scores.items():
            say(type="metric", name="ndcg@10", value=round(float(value), 5), arm=name, step=n)
        say(type="progress", fraction=round(n / rounds, 4), done=n, total=rounds, unit="rounds")

    say(type="stage", name=f"Train on {len(gt):,} queries, check on {len(gv):,}")
    booster = lgb.train(lgb_params, train, num_boost_round=rounds, valid_sets=[train, valid],
                        valid_names=["train", "validation"],
                        callbacks=[lgb.record_evaluation(history), each_round,
                                   lgb.early_stopping(patience, first_metric_only=True, verbose=False)])
    best = booster.best_iteration or booster.current_iteration()
    v_ndcg = float(history["validation"]["ndcg@10"][best - 1])
    t_ndcg = float(history["train"]["ndcg@10"][best - 1])
    print(f"stopped at {best} trees: ndcg@10 {v_ndcg:.4f} on validation queries, {t_ndcg:.4f} on training queries", flush=True)

    model = out / "model"
    model.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(model / "model.txt"), num_iteration=best)
    (model / "model.json").write_text(json.dumps(booster.dump_model(num_iteration=best), separators=(",", ":")))
    (model / "features.json").write_bytes(bundle_bytes)
    scorer = pathlib.Path(__file__).resolve().parent.parent / "features" / "model.py"
    if scorer.exists():
        shutil.copyfile(scorer, model / "rank.py")
    gain = booster.feature_importance(importance_type="gain")
    top = sorted(zip(names, (float(g) for g in gain)), key=lambda kv: -kv[1])[:5]
    result = {
        "metrics": {"ndcg@10": round(v_ndcg, 5), "ndcg@10_train": round(t_ndcg, 5), "trees": int(best),
                    "train_queries": len(gt), "valid_queries": len(gv), "rows": int(table.num_rows)},
        "params": {k: params[k] for k in DEFAULTS}, "artifact_dir": "model",
        "spec": {"rerank": {"model": "model.json", "features": "features.json", "top_k": int(params["top_k"]),
                            "trainer": "lightgbm-ltr", "feature_version": bundle.get("version")}},
        "description": f"starter ranker: lambdarank, {best} trees over {len(names)} features, trained on {ts.get('ref')}"
                       + (" (SIMULATED labels)" if bundle.get("simulated") else ""),
        "known_issues": (["Trained on simulated events: it has learned the simulator, not real shoppers."]
                         if bundle.get("simulated") else [])
                        + ["Most useful features by gain: " + ", ".join(n for n, _ in top) + "."],
    }
    (out / "result.yaml").write_text(json.dumps(result, indent=1))  # JSON is YAML, and the contract reads either


if __name__ == "__main__":
    main()
