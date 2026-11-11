# The trainer contract

Captain doesn't train your model. It runs your training command with the exact inputs you asked for, then
records what the command reports as a new pipeline version. Any language works: the contract is environment
variables in and one YAML file out.

## Declare the trainer

```yaml
spec:
  train:
    command: python train.py          # any shell command, run in an empty folder
    inputs:                           # optional; --input on the command line replaces this list
      - shop-catalog@v3
      - {ref: shop-judgments@v2, role: labels, as_of: "2026-10-01"}
    params: {learning_rate: 0.05}     # optional; --param name=value adds to it
    timeout_s: 7200                   # optional, default 3 hours
    compute: my-batch                 # optional compute target, default this machine
```

Run it with `cst pipeline retrain <name>[@vN] --local-exec [--input role=name@vN ...] [--param k=v ...] [--as-of DATE]`.
On a compute target add `--compute <target>`, and `--wait` to block until the job records the new version.

## What the command gets

| Variable | What it holds |
|---|---|
| `CST_TRAIN_INPUTS` | JSON list. Each item: `ref` (exact, `name@vN`), `kind` (catalog, events, queries, judgments, logs), `role` (yours, defaults to the kind), `uri` (the version's folder in the workspace store), `files` (each part's `path`, `uri` and `bytes`), `label_source` (`human`, `llm`, `clicks`, `simulated` or null), `as_of` (a cutoff date or null) |
| `CST_PARAMS` | JSON object: `spec.train.params`, then `--param` values on top |
| `CST_OUT` | An empty folder for your outputs |
| `CST_PIPELINE`, `CST_WORKSPACE` | The version being retrained and the workspace |
| `CST_PYTHON` | The Python Captain itself runs on. Built-in trainers start from it: `"$CST_PYTHON" -m captain_search.trainers lightgbm-ltr` |
| `CST_TRAIN_OUT` | Older trainers: a file path for the same result. Still read. If both are written, `result.yaml` wins key by key |

### An input's files

`files` is a list of `{path, uri, bytes}`, one per part. Read each part from its own `uri`:

- Parts are stored by content hash, at `<store>/objects/sha256/<first 2 hex>/<hash>`. So a part's `uri` has no file
  extension, and it is not inside the input's `uri` folder (that folder holds the version's manifest).
- `path` is the part's name in the dataset and tells you the format. A snapshot is parquet: `data/part-0.parquet`.
- A file kept by reference (a live folder, an external file) has `path: null` and the `uri` where it lives.
- A live query has `ref: null`, `uri: null`, no `files`, and `source: {connection, query}`. The trainer reads the
  source itself.

A project's run also sets `slot` (the project slot the input fills), `mode` (`snapshot` or `live`), `rows` and
`fingerprint` on each item.

`label_source` comes from the dataset: a judgments dataset's own judge (`click` reads as `clicks`, `heuristic` as
`simulated`), or a `label_source` tag, or the input's own `label_source`. `as_of` comes from the input, then the
dataset's `as_of` tag, then `--as-of`. Captain passes it on and records it. Honoring the cutoff is the trainer's job.

## What the command writes

`CST_OUT/result.yaml`, every key optional:

```yaml
metrics: {ndcg@10: 0.412, recall@100: 0.88}   # numbers stream to Runs and are stored on the version
params: {trees: 400}                          # merged over CST_PARAMS on the version
artifact_dir: model                           # relative to CST_OUT. Its files are stored with the version under artifact/
registry_ref: s3://models/arch-v2/shop/v7     # where the weights live, if not in artifact_dir
spec: {rerank: {model: lgbm}}                 # merged into the new version's spec
training_commit: 3f2a9c1
known_issues: ["cold-start queries untested"]
description: "lambdarank, 400 trees"
cost: {train_usd: 3.5, train_gpu_hours: 0.5, gpu_type: A10G}
```

Unknown keys fail the retrain, so a typo doesn't vanish. A non-zero exit fails it too, with the last lines of output.
`artifact_dir` is capped at 2 GB. Larger weights belong in your own storage, named by `registry_ref`.

The new version stores `train_inputs` (the refs, without the file lists), `train_params`, `train_metrics`,
`parents` (`[name@vN]`) and `source: {kind: retrain, command, job}`. Versions written before this contract read
those fields as empty.

## Example trainer

```python
import json, os, pathlib

out = pathlib.Path(os.environ["CST_OUT"])
inputs = {i["role"]: i for i in json.loads(os.environ["CST_TRAIN_INPUTS"])}
params = json.loads(os.environ["CST_PARAMS"])
labels = inputs["labels"]
print(f"training on {labels['ref']} ({labels['label_source']} labels, cutoff {labels['as_of']})", flush=True)
# ... read labels["files"][i]["uri"], train with params["learning_rate"] ...
(out / "model").mkdir()
(out / "model" / "weights.txt").write_text("trained weights")
score = 0.412
print(f"ndcg@10={score}", flush=True)
(out / "result.yaml").write_text(
    f"metrics:\n  ndcg@10: {score}\nartifact_dir: model\nparams:\n  trees: 400\n")
```

## On a compute target

The retrain returns at once with `{pending: true, job, live_id}` and no `pipeline` field, because the new version
doesn't exist until the job finishes. The job retrains the same version on the same exact input refs that were
resolved when it was submitted, publishes the new version to the workspace store, and writes
`jobs/<job>.result.yaml` with its ref. `cst job show <job>` then includes `result`. `--wait` (and the
`retrain-and-review` workflow, which sets it) polls the job and returns the new version, or fails if the job failed
or recorded nothing.

## The built-in starter trainer

`lightgbm-ltr` is a trainer like any other, kept in the package as a working example
(`captain_search/trainers/lightgbm_ltr.py`). Its input is a training set from `cst data training-set`, given with
role `training`: a parquet part with `query_id`, `grade`, `split`, `event_ts`, `request_id`, `label_kind` and one
column per feature, plus `features.json`,
which names the features and carries the lookup tables scoring needs. It trains LightGBM lambdarank, stops early on
the validation queries, prints one JSON line per round (`{"type": "metric", "name": "ndcg@10", ...}`), and writes
`model/` with the model, the unchanged `features.json` and a scorer that needs only numpy.

LightGBM is not part of Captain's base install or its app. The `train-ltr` extra holds the pin
(`pip install 'captain-search[train-ltr]'`, which the runner image does). Without it, the first run makes a private
environment under Captain's home folder (`envs/train-ltr`), which needs the internet once.

`cst projects starter-train <project>` runs all of it for a project: the keyword baseline if there is none, the
training set, the trainer, and the new candidate.

Each training row keeps the time and request id of the search it came from (`event_ts`, `request_id`), so features
logged at serving time can be matched back to it. `label_kind` is `clicks`, `judgment` or `unshown`. Unshown rows
are weak negatives: results the first stage finds that the old ranker never showed for the query, added as grade 0
so the ranker trains on the same top 50 it reorders. The training set's provenance says how each feature is timed:
click features are counted once, up to one cutoff, and the rest are computed per row.
