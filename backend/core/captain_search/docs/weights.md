# Weights

A network's weights are a program. This guide covers how a person or an agent opens a small network in Captain,
reads it, changes it, and gets an exact record of what was allowed and what changed.

Everything here is one set of operations with three faces: Python functions in `captain_search.weights`, routes
under `/api/weights/`, and `cst weights` commands. Every command takes `--json` (before the command:
`cst --json weights layers`) and prints the same shape the route returns.

Only fully connected networks are supported. The code is numpy alone.

## Words used in every shape

| Word | Meaning |
|---|---|
| column | A column of neurons. Column 0 is the input. Column 1 is the first layer after it. |
| layer | The weights and biases between two columns. Layer `k` joins column `k` to column `k + 1`, counted from 0. |
| `W[to][from]` | One layer's weights. Row `to` holds the weights coming into neuron `to` of column `k + 1`. |
| tensor | A layer's weight table or bias list by name: `layer1.weight`, `layer1.bias`. |
| `z` | A neuron's weighted sum, before the activation function. |
| `a` | A neuron's activation, after it. |
| `dcost_da` | How much the cost changes per unit change of that activation. |
| `wanted_nudge` | The negative of `dcost_da`. Positive means the cost falls if the neuron is more active. |
| examples | A couple of dozen labelled inputs that come with a model, for a person to pick and look at. They are never part of a score. |
| `dcost_dz`, `da_dz`, `a_before` | The other chain-rule numbers. A weight's gradient is `a_before * da_dz * dcost_da`. |

**Counting.** In every field, here and in `--json`, layers, neurons, outputs and examples count from 0. The one
exception is `layer` when it names a neuron's place (in an edit, an event and `touched.neurons`): there it is the
column, so 1 is the first layer after the input. In every sentence a person reads (an event's `sentence`, what
`cst weights` prints without `--json`, a refusal) layers and neurons count from 1: "layer 2, neuron 7".

## The two starter networks

Captain makes both the first time weights are opened in a workspace. Nothing is downloaded.

- `tiny` is 2-3-1, 13 numbers. Two inputs between 0 and 1. The output says whether exactly one of them is above
  one half.
- `digits` is 784-16-16-10, 13,002 numbers. It reads a 28 by 28 picture and names a digit.
  **Its data is synthetic.** The pictures are drawn by code from stroke templates with jitter and noise. They are
  not handwriting and not MNIST. A score on them says nothing about real handwriting.

## A model on disk

A model is a manifest plus tensors. In a workspace it lives at `weights/<model>/v<N>/`.

```json
{
  "format": "captain-weights/1",
  "name": "tiny",
  "kind": "dense",
  "sizes": [2, 3, 1],
  "cost": "cross_entropy",
  "layers": [
    {"name": "layer1", "inputs": 2, "outputs": 3, "activation": "sigmoid", "weight": "layer1.weight", "bias": "layer1.bias"},
    {"name": "layer2", "inputs": 3, "outputs": 1, "activation": "sigmoid", "weight": "layer2.weight", "bias": "layer2.bias"}
  ],
  "inputs": {"meaning": "two numbers between 0 and 1", "names": ["x1", "x2"]},
  "outputs": {"meaning": "1 when exactly one input is above one half", "names": ["exactly one is high"]},
  "tensors": {
    "layer1.weight": {"file": "tensors.safetensors", "shape": [3, 2], "dtype": "float32"},
    "layer1.bias": {"file": "tensors.safetensors", "shape": [3], "dtype": "float32"}
  },
  "data": {"synthetic": true, "note": "points drawn by code with a seed"},
  "parameters": 13
}
```

Activations: `sigmoid`, `relu`, `tanh`, `linear`, and `softmax` on the last layer. Costs: `squared_error` (the sum
over outputs of the squared difference, averaged over examples) and `cross_entropy`. Tensor files are `.safetensors`
or `.npz`. A picture input adds `"shape": [28, 28]` under `inputs`.

A version saved from a session holds no tensors of its own. Its manifest names its base and carries the patches:
`"base": "tiny@v1"`, `"patches": [...]` and `"record"`, the session record. Opening it applies the patches to the
base.

## A session

Every read and every change happens in a session. The session records who is working, what they were allowed,
and what they used. The record is written by the tools. A caller cannot set it.

```
cst weights open tiny --agent --name claude --forward-passes 20 --no-sandbox
```

`POST /api/weights/sessions` with `{"model": "tiny", "by": "agent", "name": "claude", "raw_weights": true,
"sandbox": false, "forward_passes": 20}` returns a session:

```json
{
  "id": "ws-3f9a1c2e",
  "model": "tiny",
  "version": 1,
  "who": {"kind": "agent", "name": "claude"},
  "ledger": {
    "raw_weights": true, "weights_read": 0,
    "sandbox": false, "sandbox_used": false,
    "forward_allowed": 20, "forward_used": 0, "pass_inputs": 32,
    "gradients_computed": false, "evaluations": 0, "training_steps": 0,
    "changed": 0, "total": 13, "share": 0.0,
    "feedback": true, "scores_shown": false, "first_score": null,
    "screen_looks": 0, "screen_numbers": 0,
    "weights_alone": true,
    "label": "from the weights alone"
  },
  "sets": {
    "target": {"defined": false},
    "should_not_change": {"defined": false},
    "held_out": {"defined": true, "of": 300, "says": "the model's held-out set"}
  },
  "edits": [],
  "events": [],
  "event_count": 1,
  "saved": [],
  "closed": false
}
```

`weights_alone` is true only when all three hold: no sandbox was allowed, no forward pass was used, and no score
was shown to the caller before its last edit. It is worked out from the record each time. The label is one of
"from the weights alone", "from the weights, with code, network not run", "from the weights, with scores shown
along the way" or "with 3 of 20 forward passes". A training step adds ", and 1 training step".

**Scores are feedback.** A score shown after an edit is information from running the network. So:

- In an ordinary session an edit, an undo, a redo and `evaluate` return their scores. The first one returned
  ends "from the weights alone". `ledger.first_score` names the event and the operation that did it.
- A session opened blind (`"feedback": false`, `cst weights open <model> --blind`) measures and stores every
  score and shows none. An edit's event comes back without `cost`, `cost_before`, `cost_after` or `predicted`
  and with `"scores": "hidden"`. `evaluate` answers `{"measured": true, "scores": "hidden", "says": "..."}`.
  No route shows them. When the session is saved (`save`) or closed (`close`), every stored score is shown from
  then on. Only a blind session can make edits and keep the label. Editing again after the scores are shown
  ends it.

A person's session gets 50 forward passes unless told otherwise. An agent's gets none: say how many with
`--forward-passes`. `events` in a session answer holds the last 100. The whole record is in `report`.

Until the sets are named with `sets`, an edit is scored on the held-out set alone.

### Something refused

A run past the allowance, raw weights the session may not see, or an export without a sandbox answers with status
403 and is written into the record:

```json
{"error": "All 20 forward passes of this session are used. The network cannot be run again in it.", "refused": true}
```

`cst weights` prints the sentence and exits with code 3. Anything else that cannot be done (no such neuron, an
edit that changes nothing) is status 400 with `{"error": "..."}` and exit code 1. A missing model or session is 404.

### A screen looking

Add `watch=1` to a read of the weights (`layers`, `read`, `stats`, `heatmap`, `template`, `diff`, and
`GET /api/weights/models/{model}?session=`) when a screen shows a session to a person looking on. The look is not
an event, is not held to the session's allowances, and does not tell the feed. It is counted beside the record:
`ledger.screen_looks` is how many looks there were and `ledger.screen_numbers` how many numbers they showed.

Nothing that runs the network takes `watch`. `forward`, `activations`, `gradients`, `trace` and `slice` always
count against the session. A screen works out activations and gradients itself from the weights it was sent.

`cst weights` commands act on the session opened last. Name another with `--session ws-...`.

## Operations

Each line: the command, the route, and what comes back. `{s}` is the session id.

### Read

| Command | Route | Returns |
|---|---|---|
| `cst weights models` | `GET /api/weights/models` | `[{id, name, says, sizes, parameters, versions, synthetic}]` |
| `cst weights show <model>` | `GET /api/weights/models/{model}` | the whole network, see below |
| `cst weights open <model>` | `POST /api/weights/sessions` | a session |
| | `GET /api/weights/models/{model}/session` | the newest session on the model, made if there is none |
| `cst weights sessions <model>` | `GET /api/weights/models/{model}/sessions` | `[{id, model, version, who, opened_at, events, edits}]`, newest first |
| `cst weights assign <model> <project>` | `POST /api/weights/models/{model}/project` | `{model, project}`. Body: `{"project": "shop"}` |
| `cst weights import <folder> [--name]` | `POST /api/weights/import` | `{model, version, ref, parameters, sizes, cost_points}`. Body: `{"folder": "...", "name": "..."}` |
| `cst weights session` | `GET /api/weights/sessions/{s}` | a session |
| `cst weights layers` | `GET /api/weights/sessions/{s}/layers` | `{sizes, cost, parameters, layers: [...]}` |
| `cst weights read <tensor> [--rows 0:2] [--cols 0:4]` | `GET /api/weights/sessions/{s}/read?tensor=&rows=&cols=` | `{tensor, shape, rows, cols, values, count}` |
| `cst weights stats [<tensor>] [--neurons]` | `GET /api/weights/sessions/{s}/stats?tensor=&neurons=1` | per tensor and per neuron numbers |
| `cst weights heatmap [--layer 0] [--neuron 3]` | `GET /api/weights/sessions/{s}/heatmap?layer=&neuron=` | one grid per neuron |
| `cst weights template <output>` | `GET /api/weights/sessions/{s}/template?output=` | an output's incoming weights |

The whole network, for a screen (`GET /api/weights/models/{model}`, at most 200,000 numbers):

```json
{"id": "tiny", "name": "tiny", "version": 1, "sizes": [2, 3, 1], "squish": "sigmoid", "cost": "cross_entropy",
 "layers": [{"W": [[5.1, -4.9], [4.8, 5.0], [-3.2, 3.1]], "b": [0.2, -7.1, 0.4], "activation": "sigmoid"}],
 "inputNames": ["x1", "x2"], "outputNames": ["exactly one is high"],
 "examples": [{"name": "example 1: 1", "x": [0.9, 0.1], "y": [1.0]}],
 "test": [], "synthetic": true}
```

`examples` are the model's couple of dozen labelled inputs for a person to pick and look at (24 for `tiny`, 40
for `digits`). They are never part of a score. `test` is always empty: the held-out examples are never sent.
Scores on them come from `evaluate`. Add `?session={s}` to get the numbers as the session has edited them.

`layers`:

```json
{"sizes": [2, 3, 1], "cost": "cross_entropy", "parameters": 13,
 "layers": [{"layer": 0, "name": "layer1", "inputs": 2, "outputs": 3, "activation": "sigmoid",
             "weight": {"tensor": "layer1.weight", "shape": [3, 2]}, "bias": {"tensor": "layer1.bias", "shape": [3]},
             "parameters": 9}]}
```

`read` is refused when the session may not see raw weights. Every number it returns is counted in `weights_read`.

```json
{"tensor": "layer1.weight", "shape": [3, 2], "rows": [0, 2], "cols": [0, 2], "values": [[5.1, -4.9], [4.8, 5.0]], "count": 4}
```

`stats`, weights and biases apart:

```json
{"tensors": [{"tensor": "layer1.weight", "shape": [3, 2], "count": 6, "mean": 1.65, "variance": 17.2,
              "min": -4.9, "p25": -2.1, "median": 3.9, "p75": 5.0, "max": 5.1, "norm": 10.9,
              "zero_share": 0.0, "distinct": 6}],
 "neurons": [{"layer": 0, "neuron": 0, "bias": 0.2, "weight_norm": 7.07, "weight_mean": 0.1,
              "weight_min": -4.9, "weight_max": 5.1, "zero_share": 0.0, "dead": false}],
 "bias_range": {"first": 7.5, "last": 0.0}}
```

`heatmap`, a layer's weights per input as a grid. For a picture input the grid has the picture's shape:

```json
{"layer": 0, "grid_shape": [28, 28], "scale": 1.9,
 "grids": [{"neuron": 3, "bias": -0.4, "min": -1.9, "max": 1.2, "grid": [[0.0, 0.01], [0.3, -0.2]]}]}
```

`scale` is the largest size of any weight shown, for one colour scale across the grids. `template` is the same
picture for one output neuron: `{layer, output, name, bias, weights, grid_shape, grid}`.

### Run

Each of these runs the network, so each costs one forward pass from the session's allowance. Past the allowance
they are refused. One pass may hold up to `pass_inputs` inputs.

| Command | Route | Returns |
|---|---|---|
| `cst weights forward --input 0.9,0.1` or `--example 3` | `POST /api/weights/sessions/{s}/forward` | outputs and the passes left |
| `cst weights activations --example 3` | `POST /api/weights/sessions/{s}/activations` | `z` and `a` for every neuron |
| `cst weights gradients --example 3` | `POST /api/weights/sessions/{s}/gradients` | the cost and every gradient |
| `cst weights trace --example 3 [--path 0,2,0]` | `POST /api/weights/sessions/{s}/trace` | the chain rule along one path |
| `cst weights slice <tensor> <index> --example 3` | `POST /api/weights/sessions/{s}/slice` | the cost as one number moves |

Bodies take `{"inputs": [[0.9, 0.1]], "targets": [[1]]}` or `{"example": 3}` (one of the model's examples, by
number from 0).

```json
{"outputs": [[0.97]], "predicted": [1], "cost": 0.03, "forward_used": 1, "forward_left": 19}
```

`activations`:

```json
{"input": [0.9, 0.1], "columns": [{"column": 1, "z": [4.1, -2.3, -2.6], "a": [0.98, 0.09, 0.07]}],
 "output": [0.97], "predicted": 0, "wanted": [1.0], "cost": 0.03, "forward_used": 2, "forward_left": 18}
```

`cost_terms` says what each output adds to this example's cost: `[{"output": 0, "a": 0.97, "wanted": 1.0, "term":
0.03}]`. The terms add up to `cost`.

`gradients` marks the session "gradients computed":

```json
{"cost": 0.03, "examples": 1,
 "layers": [{"layer": 0, "name": "layer1", "weight": [[-0.01, -0.001]], "bias": [-0.012],
             "dcost_da": [-0.14], "wanted_nudge": [0.14], "dcost_dz": [-0.012], "da_dz": [0.02]}],
 "input_dcost_da": [-0.06, 0.06],
 "top": [{"tensor": "layer2.weight", "index": [0, 0], "value": 7.2, "gradient": -0.029}],
 "forward_used": 3, "forward_left": 17}
```

A weight table with more than 4,096 numbers is left out of `layers[].weight` (it is `null`) and its largest
entries are in `top`. Ask for one neuron's row with `"layer": 0, "neuron": 3` to get `row`: `{layer, neuron,
tensor, gradient, bias_gradient}`.

With `"layer": 1` or higher the answer also has `desires`: how the neurons of the column before that layer come
to want what they want. Each neuron of the layer asks, through its weight, and the asks add up.

```json
{"desires": {"layer": 1, "of_column": 1, "asked_by_column": 2, "cells": [[-0.02], [0.01], [0.03]],
             "sums": [-0.02, 0.01, 0.03], "exact": true}}
```

`cells[i][j]` is what neuron `j` of column 2 asks of neuron `i` of column 1. `sums[i]` is that neuron's
`dcost_da`. `exact` is false when several examples were averaged.

`trace`:

```json
{"path": [0, 2, 0], "cost": 0.03,
 "steps": [{"layer": 0, "neuron": 2, "from_neuron": 0,
            "weight": {"tensor": "layer1.weight", "index": [2, 0], "value": -3.2},
            "bias": {"tensor": "layer1.bias", "index": [2], "value": 0.4, "gradient": 0.004},
            "z": -2.6, "a": 0.07, "a_before": 0.9, "da_dz": 0.065, "dcost_da": 0.07, "dcost_dz": 0.0045,
            "gradient": 0.004, "product": 0.004, "product_is_exact": true}],
 "path_product": 0.004, "first_weight_gradient": 0.004, "paths_from_first_weight": 1}
```

For softmax `product_is_exact` is false, because softmax ties its outputs together. The gradient is then
`a_before * dcost_dz`.

`slice` moves one number over a range, holds the rest still, and gives the cost at each value. Body: `{"tensor":
"layer1.weight", "index": [2, 0], "example": [0, 1, 2], "span": 4, "points": 21}`. It runs the network once per
point, so it costs `points` forward passes, and it is refused whole when fewer are left. Nothing is changed.

```json
{"tensor": "layer1.weight", "index": [2, 0], "value": -3.2, "cost": 0.03, "slope": 0.004,
 "values": [-5.2, -5.0], "costs": [0.09, 0.08], "examples": 3, "dimensions": 13, "forward_used": 24, "forward_left": 26}
```

`dimensions` is how many numbers the network has. The picture is a slice through one of them.

### Change

| Command | Route | Returns |
|---|---|---|
| `cst weights sets --target 9 --keep 0,1,2` | `POST /api/weights/sessions/{s}/sets` | the three sets |
| `cst weights edit <op> ... --why "..."` | `POST /api/weights/sessions/{s}/edits` | an event |
| `cst weights diff` | `GET /api/weights/sessions/{s}/diff` | what differs from the base |
| `cst weights undo [<edit>]` | `POST /api/weights/sessions/{s}/undo` | an event |
| `cst weights redo [<edit>]` | `POST /api/weights/sessions/{s}/redo` | an event |
| `cst weights evaluate` | `POST /api/weights/sessions/{s}/evaluate` | the three sets and the drift |
| `cst weights save --as <note>` | `POST /api/weights/sessions/{s}/save` | the new version |
| `cst weights close` | `POST /api/weights/sessions/{s}/close` | the session, closed. Nothing more can be run or changed in it |
| `cst weights report` | `GET /api/weights/sessions/{s}/report` | the whole record |
| `cst weights events` | `GET /api/weights/sessions/{s}/events?since=` | the short event list |
| `cst weights export <file>` | `POST /api/weights/sessions/{s}/export` | writes the tensors to a file |

An edit names a neuron or a tensor:

```json
{"op": "set", "layer": 1, "neuron": 2, "from": null, "value": -1.5, "why": "the bias keeps this unit off", "by": "agent"}
{"op": "scale", "layer": 1, "neuron": 2, "factor": 0.5}
{"op": "zero", "layer": 1, "neuron": 2}
{"op": "add", "tensor": "layer1.weight", "indices": [[2, 0], [2, 1]], "values": [0.1, -0.1]}
{"op": "set", "tensor": "layer2.bias", "indices": [[0]], "values": [0.3]}
{"op": "add_direction", "tensor": "layer1.weight", "direction": [1, -1], "scale": 0.2, "neurons": [0, 2]}
```

Here `layer` is the neuron's column, so 1 is the first layer after the input. `from` is the incoming weight, or
`null` for the bias. `scale` multiplies a neuron's incoming weights. `zero` clears its weights and its bias.
`add_direction` adds `scale * direction` to the incoming weights of each named neuron (all of them when none is
named). With `"along": "outputs"` the direction has one number per neuron of the layer and is added to the weights
leaving each named input. An agent must give `why`.
`{"op": "step", "example": [0, 1, 2], "rate": 0.5}` takes one gradient step on the model's examples. It is an edit like
any other, costs a forward pass, and puts "training step" in the session's label.

The event that comes back, also what `events` lists:

```json
{"id": "e4", "seq": 4, "at": "2026-11-13T09:12:44Z", "by": "agent", "kind": "edit", "op": "set",
 "tensor": "layer1.bias", "layer": 1, "neuron": 2, "why": "the bias keeps this unit off",
 "touched": {"neurons": [[1, 2]], "weights": []},
 "changes": [{"k": 0, "j": 2, "i": null, "old": 0.4, "new": -1.5}],
 "changed": 1, "share": 0.0769,
 "cost": {"target": {"name": "target", "of": 40, "before": 0.61, "after": 0.22, "right_before": 21, "right_after": 37, "answers_changed": 16},
          "keep": {"name": "should not change", "of": 160, "before": 0.08, "after": 0.09, "right_before": 158, "right_after": 158, "answers_changed": 0},
          "test": {"name": "held out", "of": 200, "before": 0.19, "after": 0.12, "right_before": 179, "right_after": 195, "answers_changed": 16}},
 "sentence": "The agent set 1 number in layer1.bias (layer 1, neuron 3). Cost on the target went from 0.61 to 0.22."}
```

Once a session has computed gradients, an edit's event also has `predicted`: what the gradient on the target set
said the edit would do, beside what it did. A large `gap` means the edit was not small.

```json
{"predicted": {"on": "target", "change": -0.12, "after": 0.49, "actual_change": -0.39, "gap": -0.27}}
```

`kind` is one of `open`, `read`, `run`, `gradients`, `edit`, `undo`, `redo`, `evaluate`, `save`, `export`, `sets`,
`close`, `refused`. Every event has `sentence`. Events that scored something have `cost_before` and `cost_after`.
A run's own cost is `run_cost`.

In an edit's `cost`, `before` is the network just before this edit and `after` is just after it. A set that is not
defined is left out of `cost`. `touched.neurons` are `[column, index]`. `touched.weights` are `[layer, to, from]`. `changes` and
`touched.weights` list at most 200 entries. `changed` is the true count. An undone edit stays in the list with
`"undone": true`, and the undo is an event of its own with `"undoes": "e4"`.

`undo` with no edit named takes back the last edit still applied. An edit other than the last can be taken back
only if no later edit changed the same numbers. The refusal says which: "Take back edit e5 first: it changed the
same numbers after edit e3." Undo and redo put back the exact old numbers.

`diff`:

```json
{"base": "tiny@v1", "changed": 1, "total": 13, "share": 0.0769,
 "by_tensor": [{"tensor": "layer1.bias", "changed": 1, "total": 3, "share": 0.3333, "largest_change": 1.9}],
 "changes": [{"tensor": "layer1.bias", "k": 0, "j": 2, "i": null, "old": 0.4, "new": -1.5}]}
```

Diff is only offered between a network and its own edits. `cst weights compare a@v1 b@v1` (`GET
/api/weights/compare?a=&b=`) answers for two versions of one line of edits and refuses two networks that were
trained separately.

`sets` names the three sets every edit is measured on. Each is drawn from the model's evaluation pool, which the
session never sees:

```json
{"target": {"classes": [9]}, "should_not_change": {"classes": [0, 1, 2, 3, 5, 6, 7, 8]}}
```

`target` is what the edit should fix. `should_not_change` holds the neighbours it must leave alone. The held-out
set is the model's own and cannot be changed. A set may also be given whole as `{"inputs": [...], "targets":
[...]}`. The model's examples cannot be made into a scored set.

`evaluate`:

```json
{"sets": {"target": {"name": "target", "defined": true, "of": 40, "before": 0.61, "after": 0.22,
                     "right_before": 21, "right_after": 37, "answers_changed": 16},
          "should_not_change": {"name": "should not change", "defined": true, "of": 160, "before": 0.08, "after": 0.09,
                                "right_before": 158, "right_after": 158, "answers_changed": 0},
          "held_out": {"name": "held out", "defined": true, "of": 200, "before": 0.19, "after": 0.12,
                       "right_before": 179, "right_after": 195, "answers_changed": 16}},
 "drift": [{"layer": 0, "column": 1, "mean_change": 0.031, "relative": 0.08, "similarity": 0.996}],
 "drift_on": "held out", "before_is": "tiny@v1", "evaluations": 3}
```

`before` is the base and `after` is the base with the edits now applied, on the same examples. `drift` says how
far each column's activations moved from the base on the same inputs: the mean size of the change, the change
relative to the base's size, and the cosine similarity. Evaluations are run by the tool and are counted apart from
forward passes, as `ledger.evaluations`. In a blind session the answer holds no scores (see "Scores are feedback").

`save` writes a new version that is the base plus the applied patches, with the record attached:

```json
{"model": "tiny", "version": 2, "ref": "tiny@v2", "base": "tiny@v1", "patches": 1, "changed": 1, "share": 0.0769,
 "label": "with 3 of 20 forward passes", "weights_alone": false, "note": "", "scores_shown_now": false}
```

## A pinned neuron

The page can pin a network, a neuron or one weight for an agent. The ref is `weights:<model>`,
`weights:<model>@layer=2,neuron=6` or `weights:<model>@layer=2,neuron=6,from=3`, with `layer` from 1 and `neuron`
and `from` from 0. `cst context show` then gives the model, the layer and neuron counted from 1, its bias, its
five largest incoming and outgoing weights, and the `cst weights` commands that read more.

## Live

Every operation tells the app's change feed under `weights:<model>` and `weights-session:<id>`. A screen listens
to both and reads the session again. `cst weights` in a terminal is heard too.

## Bring your own training

See `cst docs trainer-contract`, "Writing a network", which has a ten-line example. In short: the training
command writes a manifest and tensors into a folder under `CST_OUT`, names it in `result.yaml` as `network_dir`,
and may append one JSON line per step to `CST_OUT/cost.jsonl`. Captain stores the network as a weights model
version and puts each cost point on the live run as it is written. `cst weights import <folder> --name <model>`
brings in a folder made any other way. A model's own sets come along as `.npz` files with `x` and `y`. The cost
history is kept in the manifest under `training.cost` as `[step, cost]` pairs.

## Limits

- Dense networks only. No convolutions, no attention.
- The whole-network route sends at most 200,000 numbers.
- The digits starter is trained on synthetic pictures.
- Two networks trained separately cannot be compared weight by weight. The same network can be written with its
  neurons in another order, so the difference means little.

Sources: the operation list follows the builder brief of 2026-11-12 for the Weights workbench.
