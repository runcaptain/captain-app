#!/usr/bin/env python3
"""Shot check (SHOT_ASLEEP=1): the project's tuned ranker staged on Modal beside its baseline, and asleep, without
calling Modal. A compute plugin in a scratch project folder takes the place of the Modal web endpoint kind and says
no copy is up, the serving setup and its compute target are written as setup would write them, and the staging
record is written as a real deploy on 2026-11-11 left it. Run with cst's own Python and CST_HOME:
seed-asleep.py <project id> <project folder>."""
import datetime as dt
import os
import sys

from captain_search import compute as C
from captain_search import ops
from captain_search import project_state as PS
from captain_search.staging import _save, cost, key_name
from captain_search.staging.hosts import get as host
from captain_search.workspace import open_context

pid, folder = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(folder, "compute"), exist_ok=True)
with open(os.path.join(folder, "captain.toml"), "w") as f:
    f.write('name = "shot-asleep"\n')
with open(os.path.join(folder, "compute", "asleep.py"), "w") as f:
    f.write('''"""A stand-in for Modal in the shot check: the endpoint exists and no copy is up. Nothing is called."""
from captain_search.compute_providers.modal_serving import ModalServingProvider
from captain_search.extensions import compute


@compute("modal-endpoint")
class AsleepOnModal(ModalServingProvider):
    def staged_status(self, rec):
        return {"state": "running", "copies": 0}

    def invoke(self, rec, payload):
        raise ValueError("the shot check's stand-in is asleep and answers nothing")

    def unstage(self, resources):
        return []
''')

ctx = open_context()
storage = f"modal://captain-serving/{ctx.workspace.name}"
C.add(ctx, "modal-serving", "modal-endpoint", None, None, {"name_prefix": "captain", "serving_storage": storage, "idle_seconds": "300"})
h = host("modal")
now = dt.datetime.now(dt.timezone.utc)
h.write(ctx, {"cloud": "modal", "status": "ready", "target": "modal-serving", "storage": storage, "names": {"volume": "captain-serving", "apps": "captain"},
              "created": [{"type": "modal-volume", "name": "captain-serving"}, {"type": "compute-target", "name": "modal-serving"}],
              "created_at": now.isoformat(timespec="seconds"), "created_by": "shot"})
doc = PS.get(ctx, pid)
champ = doc.get("champion")
cand = next((e["pipeline"] for e in doc.get("experiments") or [] if e["pipeline"] != champ), None) or ops.ref(ctx.registry.resolve("pipeline", "demo-tuned")[0])
t = C.get(ctx, "modal-serving")
name = f"captain-{pid}-staging"[:60]
_save(ctx, pid, "staging", {
    "env": "staging", "status": "running", "target": "modal-serving", "kind": "modal-endpoint", "how": "captain", "id": "stg-shotasleep1",
    "bundle": f"staging/{pid}/bundles/stg-shotasleep1", "model_version": cand, "arms": {"candidate": cand, **({"baseline": champ} if champ else {})},
    "serve": "candidate", "experiment": None, "started_at": (now - dt.timedelta(hours=3, minutes=12)).isoformat(timespec="seconds"), "started_by": "shot",
    "stopped_at": None, "stop_reason": None, "cost": cost(t, ctx), "idle_minutes": 240, "scale_to_zero": True, "serving_storage": storage,
    "resources": [{"type": "modal-app", "name": name}, {"type": "modal-secret", "name": name}], "tried": True,
    "endpoint": {"name": name, "url": f"https://example--{name}-endpoint-web.modal.run"}, "error": None, "check": None,
    "key_secret": key_name(pid, "staging"), "previous": None, "previous_bundle": None})
print(pid)
