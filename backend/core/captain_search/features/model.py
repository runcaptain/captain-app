"""Score rows with a trained gradient-boosted tree model, without the library that trained it.

The starter trainer writes LightGBM's own JSON dump of the model (`model.json`). This file reads that dump and
walks the trees with numpy, so the app and its offline evaluations can rank with the model while LightGBM stays out
of the app bundle. It is copied into every trained artifact as `rank.py`, so the artifact scores on its own:

    python rank.py model.json rows.json      # rows.json: a list of feature rows, in the model's feature order

It needs only numpy and imports nothing from Captain. It handles numeric splits ("<="), with missing values
going the way each split says. A model with categorical splits is refused rather than scored wrongly.
"""
from __future__ import annotations

import json
import sys

import numpy as np


class TreeModel:
    def __init__(self, dump: dict):
        self.names = list(dump.get("feature_names") or [])
        self.trees = [self._flatten(t["tree_structure"]) for t in dump.get("tree_info") or []]
        gain: dict[int, float] = {}

        def walk(node: dict) -> None:
            if "split_feature" in node:
                f = int(node["split_feature"])
                gain[f] = gain.get(f, 0.0) + (float(node.get("split_gain") or 0.0) or 1.0)
                walk(node["left_child"])
                walk(node["right_child"])

        for t in dump.get("tree_info") or []:
            walk(t["tree_structure"])
        total = sum(gain.values()) or 1.0
        name = lambda i: self.names[i] if i < len(self.names) else f"feature {i}"  # noqa: E731
        self._importance = {name(i): round(g / total, 4) for i, g in sorted(gain.items(), key=lambda kv: -kv[1])}

    @staticmethod
    def _flatten(root: dict):
        feat, thr, left, right, value, nan_left = [], [], [], [], [], []

        def add(node: dict) -> int:
            i = len(feat)
            for a in (feat, thr, left, right, value, nan_left):
                a.append(0)
            if "leaf_value" in node and "split_feature" not in node:
                feat[i], value[i], left[i], right[i] = -1, float(node["leaf_value"]), i, i
                return i
            if node.get("decision_type") != "<=":
                raise ValueError("this model has a categorical split, which this scorer doesn't read")
            if node.get("missing_type") == "Zero":
                raise ValueError("this model treats zero as missing, which this scorer doesn't read")
            feat[i], thr[i] = int(node["split_feature"]), float(node["threshold"])
            nan_left[i] = 1 if node.get("default_left", True) else 0
            left[i] = add(node["left_child"])
            right[i] = add(node["right_child"])
            return i

        add(root)
        return (np.array(feat), np.array(thr, dtype=float), np.array(left), np.array(right),
                np.array(value, dtype=float), np.array(nan_left, dtype=bool))

    def importance(self) -> dict[str, float]:
        """How much the model leans on each feature: its share of the total gain of every split that uses it
        (split counts when the dump has no gains). Sums to 1. Empty for a model with no splits."""
        return dict(self._importance)

    def predict(self, rows) -> list[float]:
        x = np.asarray(rows, dtype=float)
        if x.size == 0:
            return []
        if x.ndim != 2 or (self.names and x.shape[1] != len(self.names)):
            raise ValueError(f"the model reads {len(self.names)} features per row, and these rows have "
                             f"{x.shape[1] if x.ndim == 2 else 'no'} columns")
        n = x.shape[0]
        idx = np.arange(n)
        out = np.zeros(n)
        for feat, thr, left, right, value, nan_left in self.trees:
            node = np.zeros(n, dtype=int)
            while True:
                f = feat[node]
                live = f >= 0
                if not live.any():
                    break
                v = x[idx, np.where(live, f, 0)]
                go_left = np.where(np.isnan(v), nan_left[node], v <= thr[node])
                node = np.where(live, np.where(go_left, left[node], right[node]), node)
            out += value[node]
        return [float(v) for v in out]


def load(text: str | bytes) -> TreeModel:
    return TreeModel(json.loads(text))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python rank.py model.json rows.json")
    with open(sys.argv[1]) as fh:
        model = load(fh.read())
    with open(sys.argv[2]) as fh:
        print(json.dumps(model.predict(json.load(fh))))
