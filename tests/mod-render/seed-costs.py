#!/usr/bin/env python3
"""Cost render test (costs-ui-live.test.mjs): what a project, a job and a live run look like once work has run on a
cloud. Given a project id and a candidate id, it records a training run of 12 minutes on the compute target
aws-batch (the test adds it first), puts that cost on the candidate, writes two lines of project history (one with
what it cost, one with only its estimate), a finished job on aws-batch that carries the cost, and a finished live
run that names the job. Prints the job id and the live run id as JSON. Run with cst's own Python and CST_HOME."""
import json
import sys

from captain_search import compute as C
from captain_search import cost_estimates as CE
from captain_search import jobs as J
from captain_search import live as LV
from captain_search import project_state as PS
from captain_search.workspace import open_context

pid, eid = sys.argv[1], sys.argv[2]
ctx = open_context()
target = C.get(ctx, "aws-batch")
job = J.new_id()
cost = CE.note_actual(ctx, job, target, 720, kind="training", size={"how": "starter", "rows": 20000}, project=pid,
                      title="Train a starter ranker")
PS.experiment_record(ctx, pid, eid, cost=cost, train_job=job)
PS.record_change(ctx, pid, {"user": "Dana", "what": "trained a starter ranker on aws-batch", "cost": cost["text"], "job": job})
PS.record_change(ctx, pid, {"user": "Dana", "what": "started judging it on aws-batch", "estimate": "$0.02 to $0.05, 2 to 4 min on aws-batch. Estimate, prices as of 2026-11-01."})
now = J._now()
J._save(ctx, {"id": job, "kind": "starter-train", "label": "starter-train", "argv": ["-w", ctx.workspace.name, "pipeline", "retrain", "outdoor-starter"],
              "target": "aws-batch", "target_kind": "aws-batch", "region": "us-west-1", "state": "succeeded", "reason": "finished",
              "handle": None, "submitted_by": "dana@example.com", "submitted_at": now, "updated_at": now, "started_at": now,
              "finished_at": now, "exit_code": 0, "backtest": None, "cost": cost})
live = LV.start_remote(ctx, "training", "Train a starter ranker", of="outdoor-starter", where="aws-batch")
LV.note_remote(ctx, live, job=job, status="done")
print(json.dumps({"job": job, "live": live, "cost": cost["text"], "detail": cost.get("detail")}))
