# Weights

A network's weights are a program. This guide covers how a person or an agent opens a small network in Captain,
reads it, changes it, and gets an exact record of what was allowed and what changed.

Everything here is one set of operations with three faces: Python functions in `captain_search.weights`, routes
under `/api/weights/`, and `cst weights` commands. Every command takes `--json` (before the command:
`cst --json weights layers`) and prints the same shape the route returns.

Fully connected networks can be read, run and changed. A trained session model with its tree rankers can be read and
changed, and not yet run: see "A trained session model with rankers". The code is numpy alone.

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

**Counting.** Two rules, and they differ on purpose.

- What a person types and reads counts from 1. That is every sentence (an event's `sentence`, what `cst weights`
  prints, a refusal) and every `cst weights` option that names a place: `--layer`, `--neuron`, `--from`,
  `--neurons`, `--path`, `--example`, and the output given to `template`. "Layer 2, neuron 7" on the page is
  `--layer 2 --neuron 7`.
- Fields count from 0. That is everything in a route's body and answer and in `--json` output: `neuron`, `from`,
  `j`, `i`, `k`, `example`, `output`, `path`, `indices`, and `layer` where it means the layer's own number (in
  `layers`, `heatmap`, `gradients`, `trace`). `--index`, `--rows` and `--cols` count from 0 too, because they are
  positions inside a tensor.
- One field is the exception: `layer` where it names a neuron's place (in an edit's body, in an event, and the
  first number of each pair in `touched.neurons`) is the column, so 1 is the first layer after the input. It is
  the same number a person says.

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
    "held_out": {"defined": true, "of": 300, "says": "the model's held-out set",
                 "base": {"model": "tiny@v1", "cost": 0.0081, "right": 300, "of": 300}}
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

`sets.held_out.base` is how the version the session was opened on does on its own held-out set, before any edit:
its cost and how many it gets right. It is a fact about that version, not a score of the session's work, so a
screen can show it from the start and it is there in a blind session too.

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
| `cst weights show <model>` | `GET /api/weights/models/{model}` | the network's description, see below. No numbers |
| `cst weights open <model>` | `POST /api/weights/sessions` | a session |
| | `GET /api/weights/models/{model}/session` | the newest session on the model, made if there is none |
| `cst weights sessions <model>` | `GET /api/weights/models/{model}/sessions` | `[{id, model, version, who, opened_at, events, edits}]`, newest first |
| `cst weights assign <model> <project>` | `POST /api/weights/models/{model}/project` | `{model, project}`. Body: `{"project": "shop"}` |
| `cst weights import <folder> [--name]` | `POST /api/weights/import` | `{model, version, ref, parameters, sizes, cost_points}`. Body: `{"folder": "...", "name": "..."}` |
| `cst weights session` | `GET /api/weights/sessions/{s}` | a session |
| `cst weights layers` | `GET /api/weights/sessions/{s}/layers` | `{sizes, cost, parameters, layers: [...]}` |
| `cst weights read <tensor> [--rows 0:2] [--cols 0:4]`, or `cst weights read --layer 1 --neuron 3 [--bias]` | `GET /api/weights/sessions/{s}/read?tensor=&rows=&cols=` | `{tensor, shape, rows, cols, values, count}` |
| `cst weights stats [<tensor>] [--neurons]`, or `--layer 1 [--neuron 3]` | `GET /api/weights/sessions/{s}/stats?tensor=&neurons=1` | per tensor and per neuron numbers |
| `cst weights heatmap [--layer 1] [--neuron 3]` | `GET /api/weights/sessions/{s}/heatmap?layer=&neuron=` | one grid per neuron |
| `cst weights template <output>` | `GET /api/weights/sessions/{s}/template?output=` | an output's incoming weights |

A network for a screen. `GET /api/weights/models/{model}` gives the description only: sizes, each layer's
activation, what inputs and outputs mean, and the examples. It holds no weights and no biases, and `numbers` is
false. `cst weights show` prints the same.

```json
{"id": "tiny", "name": "tiny", "version": 1, "sizes": [2, 3, 1], "squish": "sigmoid", "cost": "cross_entropy",
 "numbers": false,
 "layers": [{"name": "layer1", "activation": "sigmoid", "inputs": 2, "outputs": 3}],
 "inputNames": ["x1", "x2"], "outputNames": ["exactly one is high"],
 "examples": [{"name": "example 1: 1", "x": [0.9, 0.1], "y": [1.0]}],
 "test": [], "synthetic": true}
```

The numbers come only through a session, where they are counted. Add `?session={s}&watch=1`: each layer then
also has `"W": [[5.1, -4.9], ...]` and `"b": [0.2, ...]` as the session has edited them, `numbers` is true, and
the look is added to `ledger.screen_looks` and `ledger.screen_numbers`. At most 200,000 numbers are sent this way.

`examples` are the model's couple of dozen labelled inputs for a person to pick and look at (24 for `tiny`, 40
for `digits`). `test` is always empty: the held-out examples are never sent. Scores on them come from `evaluate`.

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
| `cst weights forward --input 0.9,0.1` or `--example 4` | `POST /api/weights/sessions/{s}/forward` | outputs and the passes left |
| `cst weights activations --example 4 [--layer 1 --neuron 3]` | `POST /api/weights/sessions/{s}/activations` | `z` and `a` for every neuron |
| `cst weights gradients --example 4 [--layer 2 --neuron 1]` | `POST /api/weights/sessions/{s}/gradients` | the cost and every gradient |
| `cst weights trace --example 4 [--path 1,3,1]` | `POST /api/weights/sessions/{s}/trace` | the chain rule along one path |
| `cst weights slice --layer 1 --neuron 3 --from 1 --example 4`, or `slice <tensor> <index>` | `POST /api/weights/sessions/{s}/slice` | the cost as one number moves |

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

`dimensions` is how many numbers the network has. The picture is a slice through one of them. In a blind
session a slice answers `{"measured": true, "scores": "hidden"}` with no costs, like `evaluate`.

### Change

| Command | Route | Returns |
|---|---|---|
| `cst weights sets --target 9 --keep 0,1,2`, or `--target-example 4`, or `--target-input 0.9,0.1 --target-output 1` | `POST /api/weights/sessions/{s}/sets` | the three sets |
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
set is the model's own and cannot be changed. `--target` and `--keep` take the classes' own numbers.

**Fix this one.** A person looking at one wrong answer can make it the target. Give its inputs and its wanted
output, either to `sets` or with the edit itself:

```json
{"op": "set", "layer": 3, "neuron": 9, "from": null, "value": -1.5,
 "target": {"inputs": [[0.0, 0.1, 0.9]], "targets": [7]}}
```

`targets` is one class number per input, or one row of wanted outputs per input. That is a named target set of
one, scored like any target, and it stays the session's target until another is named. `{"target": {"example":
3}}` on `sets` copies one of the model's examples in the same way. Only the target can come from an example.
"Should not change" and held out never can.

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

## A trained session model with rankers

A training run leaves a folder: a small transformer in `sess.npz` under PyTorch's names, LightGBM rankers as text
(`rank_*.txt`, each with a `*.cols.json` naming its inputs), lookup tables (other `.npz` files) and a `model.json`.
Bring it in by its folder, or by an `s3://` address of it:

```
cst weights import ./v3 --as shop-session
cst weights import s3://bucket/path/v3 --as shop-session
```

`POST /api/weights/import` with `{"folder": "...", "name": "shop-session"}` does the same. The folder may be the
version folder (with `weights/` and `MANIFEST.yaml` inside) or the `weights` folder itself. An address is read with
this computer's default cloud login. Only the `weights/` files and `MANIFEST.yaml` are fetched, and nothing is ever
written to the address.

**What can be brought in.** `cst weights registry <folder or s3://...>` and `GET /api/weights/registry?from=` list the
version folders under a folder or an address: every folder that holds `weights/sess.npz`, with the one-line description
from its `MANIFEST.yaml`. It only lists and reads. Nothing is written there. Each row's `address` is what `import` takes.

```json
{"from": "/models", "count": 2, "listed": 2, "read_only": true,
 "versions": [{"path": "arch-a/shop/v2", "address": "/models/arch-a/shop/v2", "name": "v2", "description": "wider, two layers",
               "has_manifest": true, "files": 10}],
 "says": "2 versions can be brought in from here. Nothing was written there. Bring one in with `cst weights import <address> --as <name>`."}
```

At most 500 versions are listed, and the first 200 get their description.

The model is stored as kind `session-ranker` ("session model with rankers"). Every file is kept exactly as it came
under `weights/<model>/v<N>/files/`, and the manifest records where it came from, each file's SHA-256 and size, and
one digest over all the tensors. A version is written once. Arrays that are text, or numbers that are not learned
(`cfg`, `edges`, `cat_vocab`), are kept as `facts`, not tensors. The answer:

```json
{"model": "shop-session", "version": 1, "ref": "shop-session@v1", "kind": "session-ranker",
 "kind_says": "session model with rankers", "parameters": 2281, "width": 8, "depth": 1, "heads": 2, "tensors": 35,
 "facts": ["cat_vocab", "cfg", "edges"], "rankers": ["full", "patc", "pclick"], "tables": ["covisit", "pop7"],
 "sha256": "88342a41...", "files": {"sess.npz": "5d0c..."}, "from": "/models/v3", "says": "A session model, width 8, 1 layer, 2 heads, with 3 tree rankers."}
```

**This kind can be read and changed here, not run yet.** `forward`, `activations`, `gradients`, `trace`, `slice`,
`template`, `sets`, `evaluate`, `export` and a deploy all answer status 400 with `{"error": "This model can be read and
changed here, not run yet."}`. Its numbers can be edited, taken back and saved as a new version (see "Changing it"),
and no edit is ever measured, because measuring means running the model.

### Its parts

`GET /api/weights/models/{model}`, `cst weights show <model>` and, in a session, `cst weights layers` (`GET
/api/weights/sessions/{s}/layers`) answer with what the model is, worked out from the shapes of its tensors and
nothing else. No number is in it.

```json
{"id": "shop-session", "version": 1, "kind": "session-ranker", "kind_says": "session model with rankers", "numbers": false,
 "width": 8, "depth": 1, "heads": 2, "heads_from": "model.json", "heads_says": "2 heads, as model.json says. The shapes alone cannot tell.",
 "head_width": 4, "feed_forward": 32, "parameters": 2281,
 "declared": {"session_model": "XS (1 layer, width 8, 2 heads)", "layers": 1, "layers_agree": true},
 "parts": [
   {"id": "act", "kind": "embedding", "name": "action table", "technical": "act", "tensors": ["act.weight"], "shape": [5, 8],
    "parameters": 40, "feeds_from": [], "side": "session", "rows": 5, "width": 8, "embeds": "what kind of event it was",
    "meaning_from": "name", "says": "embedding table: one row of 8 numbers for each of 5 values. What kind of event it was."},
   {"id": "text", "kind": "projection", "name": "text projection", "tensors": ["text.weight", "text.bias"], "shape": [8, 12],
    "parameters": 104, "feeds_from": [], "side": "session", "inputs": 12, "outputs": 8},
   {"id": "L0.self_attn", "kind": "attention", "name": "attention, layer 1", "layer": 0, "width": 8, "heads": 2, "head_width": 4,
    "says": "attention: which earlier events this one looks at, and what it copies from them.",
    "tensors": ["L0.self_attn.in_proj_weight", "L0.self_attn.in_proj_bias", "L0.self_attn.out_proj.weight", "L0.self_attn.out_proj.bias"],
    "shape": [24, 8], "parameters": 288, "feeds_from": ["ln"],
    "matrices": [{"role": "query", "name": "query: what this event is looking for", "tensor": "L0.self_attn.in_proj_weight",
                  "rows": [0, 8], "shape": [8, 8], "bias": {"tensor": "L0.self_attn.in_proj_bias", "rows": [0, 8]}}]},
   {"id": "L0.ff", "kind": "feed_forward", "name": "feed-forward, layer 1", "layer": 0, "inputs": 8, "hidden": 32, "feeds_from": ["L0.norm1"]}
 ],
 "wiring_says": "The order of the parts is PyTorch's usual one for these names ...",
 "facts": [{"name": "cat_vocab", "kind": "text", "shape": [6], "count": 6, "kept": true}],
 "rankers": [{"name": "full", "objective": "lambdarank", "trees": 3, "inputs": 5, "names_from": "the cols file"}],
 "tables": [{"name": "covisit", "arrays": [{"name": "pairs", "shape": [3, 2], "dtype": "int32"}]}],
 "source": {"from": "/models/v3", "read_at": "2026-11-13T10:00:00Z", "files": {"sess.npz": {"sha256": "5d0c...", "bytes": 21034}}},
 "can": {"read": true, "edit": true, "run": false, "deploy": false, "says": "This model can be read and changed here, not run yet.",
         "edits_say": "Not measured: this model cannot be run here yet."},
 "base": null,
 "sizes": [], "layers": [], "examples": [], "test": []}
```

Every part has `id`, `kind`, `name` (plain words), `says` (one sentence), `technical`, `tensors`, `shape` (of its
first tensor), `parameters` and `feeds_from` (the ids of the parts before it). `kind` is one of `embedding`,
`projection`, `norm`, `attention`, `feed_forward`, `head`, `scale`, `other`. `side` is `session` or `candidate`.
Every tensor belongs to exactly one part, so the parts' `parameters` add up to the model's.

- An attention part has `matrices`: `query`, `key`, `value` and `output`. PyTorch stacks the first three in
  `in_proj_weight`, so each is given as a range of that tensor's rows.
- `heads` comes from `model.json` or the `cfg` note, never from shapes. When neither says, it is `null` and
  `heads_says` starts "Unknown".
- What a table embeds is a guess from its name and says so: `meaning_from` is `"name"`.
- The file holds numbers, not wiring. `feeds_from` follows PyTorch's usual order and `wiring_says` says that it is
  not confirmed.

`sizes`, `layers`, `examples` and `test` are empty: they belong to fully connected networks. `GET /api/weights/models`
gives each model's `kind` and `kind_says`.

### Reading it

Through a session, counted in its record like any read. Tensors are named, not numbered: `--layer` and `--neuron`
do not apply. Nothing here runs the model.

| Command | Route | Returns |
|---|---|---|
| `cst weights read <tensor> [--rows 0:2] [--cols 0:4]` | `GET /api/weights/sessions/{s}/read?tensor=&rows=&cols=` | a slice of any tensor: a table, a list or one number |
| `cst weights stats [<tensor>] [--neurons]` | `GET /api/weights/sessions/{s}/stats?tensor=&neurons=1` | per tensor numbers. With a tensor and `neurons=1`: `rows`, one line per row |
| `cst weights heatmap --tensor <tensor> [--rows] [--cols]` | `GET /api/weights/sessions/{s}/heatmap?tensor=&rows=&cols=` | `{tensor, shape, rows, cols, grid_shape, scale, min, max, grid}` |
| `cst weights nearest <tensor> <row> [--top 10]` | `GET /api/weights/sessions/{s}/nearest?tensor=&row=&top=` | the rows that point the same way as one row |
| `cst weights directions <tensor> [--top 5]` | `GET /api/weights/sessions/{s}/directions?tensor=&top=` | a table's largest directions |
| `cst weights attention [--layer 1] [--head 2] [--heads 2] [--matrix query_key]` | `GET /api/weights/sessions/{s}/attention?layer=&head=&heads=&matrix=` | what each head looks for and what it copies |
| `cst weights labels <tensor>` | `GET /api/weights/sessions/{s}/labels?tensor=` | what each row of a table stands for, when a saved list says |

One read or heatmap sends at most 200,000 numbers. `read` and `heatmap` need a session that may see raw weights and
are counted in `weights_read`. `nearest` and `directions` also work on any table of a fully connected network.

**Row names.** `labels` names a table's rows from a list saved beside the arrays: `<table>_vocab` (so `cat_vocab` for
`cat.s.weight`), or `edges` for a price table, whose cut points make ranges ("1.00 to 2.00"). A list with one name per
row names them. A list one short of the rows names them too, and the row left over is called "none or unknown": at the
end whose row is all near zero, or at row 0 as a guess when neither end stands out. `sure` is false for a guess, and
`labels_says` says so. Any other length names nothing. It shows no number, so it is not an event.

```json
{"tensor": "price.weight", "rows": 6, "labels": ["none or unknown", "below 1.00", "1.00 to 2.00", "2.00 to 3.00", "3.00 to 4.00", "4.00 and up"],
 "labels_from": "edges", "extra_row": 0, "sure": false,
 "labels_says": "edges holds 4 edges, which make 5 ranges for 6 rows, one row more. The extra row is taken to be row 0, called \"none or unknown\". Neither end stands out, so its place is a guess: every name may be one row off."}
```

The same names are the `label` of every row that `nearest`, `directions`, `attention` and `stats` list.

`nearest`, by cosine:

```json
{"tensor": "act.weight", "rows": 5, "width": 8, "row": 0, "label": null, "norm": 2.0,
 "nearest": [{"row": 1, "label": null, "cosine": 1.0, "norm": 4.0}], "farthest": [{"row": 2, "label": null, "cosine": -1.0, "norm": 2.0}],
 "labels_says": "", "says": "Cosine is 1 for rows that point the same way, 0 for unrelated rows, and below 0 for opposite ones."}
```

`directions` is the table's singular values, largest first. `share` is each one's part of the table's total squared
size, and `needed_for_90` is how many directions carry 90% of it:

```json
{"tensor": "pos.weight", "shape": [10, 8], "singular_values": [55.5, 0.0], "share": [1.0, 0.0], "needed_for_90": 1, "of": 8,
 "directions": [{"direction": 0, "size": 55.5, "share": 1.0, "strongest_rows": [{"row": 9, "label": null, "amount": -28.3}]}]}
```

`attention` composes one block's matrices. For head `h` with its rows `Q`, `K`, `V` of the query, key and value
matrices and its columns `O` of the output matrix: `query_key` is `Q^T K` (how much an event looking with vector `x`
attends to an event offering `y` is `x (Q^T K) y`) and `output_value` is `O V` (what is written back when an event with
vector `y` is looked at). `layer` and `head` count from 0 in the route and from 1 in the command.

```json
{"layer": 0, "part": "L0.self_attn", "width": 8, "heads": 2, "heads_from": "model.json", "head_width": 4, "run": false,
 "of": [{"head": 0, "head_width": 4,
         "query_key": {"shape": [8, 8], "size": 4.0, "top": [2.0, 2.0, 2.0, 2.0, 0.0], "rank_at_most": 4},
         "output_value": {"shape": [8, 8], "size": 6.0, "top": [3.0, 3.0, 3.0, 3.0, 0.0], "rank_at_most": 4},
         "reads": [{"table": "act", "name": "action table", "rows": 5, "row_size": 2.4, "query": 1.4, "key": 2.8, "value": 4.2,
                    "strongest": [{"row": 1, "label": null, "value": 12.0}]}]}]}
```

`size` is the matrix's overall size (its Frobenius norm) and `top` its five largest singular values. `reads` says, for
each of the session's embedding tables, the typical size of a row seen as a query, as a key, and after being copied.
It leaves out the norms and biases between, and for a layer after the first it is the direct path only. With one
`head` and `matrix=query_key` or `matrix=output_value`, that head also carries `matrix: {which, grid, scale}`. When
the files do not say how many heads there are, give `heads`.

### Changing it

The same four routes as any model: `POST /api/weights/sessions/{s}/edits`, `/undo`, `/redo` and `/save`, with `cst
weights edit`, `undo`, `redo`, `save`, `diff` and `report`. An edit names a tensor and the numbers in it. `op` is `set`,
`scale`, `zero` or `add`:

```json
{"op": "set", "tensor": "act.weight", "rows": "0", "cols": "0:3", "value": 0.5, "why": "..."}
{"op": "scale", "tensor": "act.weight", "neurons": [1], "factor": 2}
{"op": "zero", "tensor": "L0.self_attn.in_proj_bias", "rows": "8:16"}
{"op": "add", "tensor": "q.weight", "indices": [[0, 0], [7, 7]], "values": [1.0, -1.0]}
```

The numbers are named by `indices` (positions, counted from 0), by `neurons` (whole rows), or by `rows` and `cols`
ranges. With none of them the whole tensor is meant. `set` and `add` take `value` for all or `values` with one each.
`cst weights edit zero --tensor act.weight --rows 0:2 --cols 0:4` is the same from a terminal. An agent must give `why`.
Only float32 tensors are edited, so that taking an edit back returns the exact bits.

Nothing is measured. The event has no `cost`. It has `"measured": false` and `"not_measured": "Not measured: this
model cannot be run here yet."`, and its sentence ends with that:

```json
{"id": "e2", "kind": "edit", "op": "set", "tensor": "act.weight", "tensors": ["act.weight"], "why": "...",
 "changes": [{"tensor": "act.weight", "index": [0, 0], "old": 0.1257, "new": 0.5}], "changed": 3, "share": 0.0013,
 "undone": false, "measured": false, "not_measured": "Not measured: this model cannot be run here yet.",
 "ledger": {"changed": 3, "total": 2281},
 "sentence": "You set 3 numbers in act.weight. Not measured: this model cannot be run here yet."}
```

`changes` lists at most 200 numbers, each by its `index` in the tensor. Undo and redo follow the same rules as for
any model and put back the exact old numbers. `diff` and `report` have the usual shape, with `index` in place of
`k`, `j`, `i`. No score is ever shown, so the session's label stays "from the weights alone".

The base is never changed. `save` writes a new version that is the base plus the changed numbers, with the record
attached and the answer's usual fields plus `"measured": false`, `not_measured` and `sha256`. The new version keeps
no files of its own: its manifest names its `base` and `files_at`, the version whose files it reads. Opening it
applies the patches and checks the digest it was saved with.

### Its tree rankers

**These are trees, not neurons.** A tree asks yes or no questions about the inputs and ends at a leaf, and the answer
is the sum of the leaves reached. There are no weights or biases to nudge, so they have their own view. The file is
LightGBM's text format, read with plain Python.

`cst weights trees <model> [--ranker full] [--trees 3]` and `GET /api/weights/models/{model}/trees?ranker=&trees=`:

```json
{"model": "shop-session", "version": 1, "ranker": "full", "rankers": ["full", "patc", "pclick"], "names_from": "the cols file",
 "kind": "trees", "objective": "lambdarank", "trees": 3, "inputs": 5, "leaves": 7, "splits": 4, "gain": 20.0,
 "unused": [{"input": 4, "name": "never_filled", "constant": true}],
 "unused_says": "1 of 5 inputs are never asked about by any tree, so changing them cannot change the answer: never_filled.",
 "by_input": [{"input": 0, "name": "views", "splits": 1, "gain": 10.0, "share": 0.5, "trees": 1}],
 "first_trees": [{"tree": 0, "leaves": 3, "depth": 2, "shrinkage": 1.0,
                  "root": {"input": 0, "name": "views", "threshold": 0.5, "gain": 10.0, "categorical": false, "missing_goes": "left", "count": 60,
                           "left": {"input": 2, "name": "price_bucket", "threshold": 3.5, "gain": 5.0, "left": {"leaf": 0, "value": 0.1, "count": 10},
                                    "right": {"leaf": 1, "value": -0.2, "count": 20}},
                           "right": {"leaf": 2, "value": 0.3, "count": 30}}}],
 "rule": "At each question, a value at or below the threshold goes left. Anything else goes right.",
 "cannot": "A tree ranker can be read here, not changed or run."}
```

`groups` folds the inputs by the start of their names, up to the first underscore (`s1_pos` is in `s1_`), largest
gain first: `[{"prefix": "f_", "count": 2, "used": 2, "unused": 0, "splits": 2, "gain": 14.0, "share": 0.7, "inputs": [0, 1]}]`.
Each row of `by_input` and `unused` has its `group`. The names are the short codes the training used.

`cst weights rankers <model>` and `GET /api/weights/models/{model}/rankers` put a model's rankers side by side:

```json
{"model": "shop-session", "version": 1,
 "rankers": [{"name": "full", "objective": "lambdarank", "trees": 3, "inputs": 5, "used": 4, "unused": ["s1_never"]}],
 "inputs": [{"name": "s1_recency", "group": "s1_", "used_by": ["full", "pclick"], "unused_by": ["patc"],
             "by": {"full": {"splits": 1, "gain": 1.0, "share": 0.05}, "patc": {"splits": 0, "gain": 0.0, "share": 0.0}}}],
 "used_by_all": ["f_views"], "used_by_some": ["s1_recency"], "unused_by_all": ["s1_never"],
 "unused_by_all_says": "1 of 5 inputs are asked about by none of the 3 rankers (full, patc, pclick), so they change no answer: s1_never.",
 "same_inputs": true}
```

`inputs` are matched by name across the rankers. `by` is `null` for a ranker that does not have the input at all.

`by_input` lists every input, largest gain first: how many questions are asked about it (`splits`), their total
`gain` from training, its `share` of all gain, and how many trees use it. `unused` is every input with no question
at all. `constant` is true when LightGBM itself marked the input as holding one value in training. `trees` asks for
up to 20 of the first trees.

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

- Only fully connected networks are run and changed. No convolutions.
- A session model with rankers is never run. Its attention is read from its matrices, and its edits are not measured.
- The whole-network route sends at most 200,000 numbers.
- The digits starter is trained on synthetic pictures.
- Two networks trained separately cannot be compared weight by weight. The same network can be written with its
  neurons in another order, so the difference means little.

Sources: the operation list follows the builder brief of 2026-11-12 for the Weights workbench.
