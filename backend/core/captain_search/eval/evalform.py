"""What the New evaluation form can offer, said once and read from the runner's own tables, so the form never
shows a choice a run would ignore.

- Measures: the offline ones a run computes from judgments (eval/metrics.py NAMES), the traffic-weighted nDCG a
  replay of logged searches adds, and the three that only simulated shoppers give. Every run computes all it can;
  the person picks the one that decides the verdict (the pre-registered primary measure).
- Depth: how many results deep each measure looks is a setting of the evaluation (eval/metrics.py
  DEFAULT_DEPTHS), recorded with each run.
- Slices: a run always reports by the class of each question, and by traffic (head, torso, tail) when the
  questions carry weights. The runner has no setting for which.
- Limits: the ones a plan takes, with their defaults.
"""
from __future__ import annotations

from . import metrics as M

MEASURES = [
    {"id": "ndcg@10", "label": "nDCG@10", "line": "Are the best answers near the top of the first 10? The usual choice.", "needs": "answers", "depth": 10},
    {"id": "p@10", "label": "Precision@10", "line": "How many of the first 10 results are good answers.", "needs": "answers", "depth": 10},
    {"id": "r@50", "label": "Recall@50", "line": "How many of the good answers show up in the first 50.", "needs": "answers", "depth": 50},
    {"id": "mrr", "label": "MRR", "line": "How high the first good answer sits.", "needs": "answers", "depth": None},
    {"id": "zero", "label": "Searches with no results", "line": "How often a question returns nothing at all.", "needs": "answers", "depth": None},
    {"id": "ndcg@10_tw", "label": "nDCG@10, traffic-weighted", "line": "nDCG@10 where a question counts as often as it was searched.", "needs": "logs", "depth": 10},
    {"id": "ctr", "label": "Click-through rate", "line": "How often a simulated shopper clicks a result.", "needs": "shoppers", "depth": None},
    {"id": "conversion", "label": "Conversion", "line": "How often a simulated shopper buys.", "needs": "shoppers", "depth": None},
    {"id": "rpv", "label": "Revenue per visit", "line": "What a simulated shopper spends per visit.", "needs": "shoppers", "depth": None},
]
SLICES = [
    {"id": "class", "label": "By group", "line": "One result per group of questions (the group column of your questions).", "needs": "a group on each question"},
    {"id": "traffic", "label": "By traffic", "line": "Head, torso and tail questions, by how often each was searched.", "needs": "a weight on each question (logged searches have one)"},
]
# How deep a measure looks is the person's to set: each of these is a field, starting at the usual depth.
DEPTHS = [{"id": k, "label": f"{M.DEPTH_WORDS[k]} looks this many results deep", "default": v, "max": M.MAX_DEPTH} for k, v in M.DEFAULT_DEPTHS.items()]
LIMITS = [
    {"id": "mde", "label": "Smallest gain worth acting on", "default": 0.003, "line": "In the deciding measure. A smaller difference reads as no clear difference."},
    {"id": "population_size", "label": "Simulated shoppers", "default": 4000, "line": "How many shoppers replay the logged searches on each model.", "only": "logs"},
]
# Asked for by the owner, and not something a run can do today: said so rather than drawn as a dead control.
NOT_YET = [
    {"what": "Which slices", "why": "A run always reports by group and by traffic. The runner has no setting to pick among them."},
    {"what": "Which measures to compute", "why": "A run computes every measure it has the data for. You pick the one that decides the verdict."},
]


def options() -> dict:
    offline = set(M.NAMES) | {"ndcg@10_tw"}
    # A measure the runner doesn't know is never offered.
    measures = [m for m in MEASURES if m["id"] in offline or m["id"] in ("ctr", "conversion", "rpv")]
    return {"measures": measures, "slices": SLICES, "limits": LIMITS, "depths": DEPTHS, "not_yet": NOT_YET, "default_measure": "ndcg@10"}
