"""The network code: backpropagation agrees with the cost measured by nudging, and the teaching numbers are right."""
from __future__ import annotations

import numpy as np
import pytest

from captain_search.weights import net as N

CASES = [
    ([4, 5, 3], "sigmoid", "squared_error"),
    ([4, 5, 3], ["tanh", "softmax"], "cross_entropy"),
    ([3, 4, 4, 1], ["relu", "tanh", "sigmoid"], "cross_entropy"),
    ([3, 4, 2], ["sigmoid", "linear"], "squared_error"),
    ([3, 4, 3], ["relu", "softmax"], "squared_error"),
]


def _data(sizes, seed=1, n=6):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, sizes[0]))
    y = np.eye(sizes[-1])[rng.integers(0, sizes[-1], n)] if sizes[-1] > 1 else rng.integers(0, 2, (n, 1)).astype(float)
    return x, y


@pytest.mark.parametrize("sizes,acts,cost", CASES)
def test_backpropagation_matches_the_gradient_measured_by_nudging_each_number(sizes, acts, cost):
    net = N.init(sizes, acts, cost, seed=3)
    for ly in net.layers:
        ly.bias = np.random.default_rng(5).standard_normal(ly.bias.shape) * 0.3
    x, y = _data(sizes)
    got, want = N.backprop(net, x, y), N.numerical_gradient(net, x, y)
    for g, w in zip(got["layers"], want):
        assert np.allclose(g["weight"], w["weight"], atol=1e-6), g["name"]
        assert np.allclose(g["bias"], w["bias"], atol=1e-6), g["name"]


def test_a_neurons_nudge_is_the_cost_slope_for_its_activation():
    net = N.init([3, 4, 2], "sigmoid", "squared_error", seed=2)
    x, y = _data([3, 4, 2], n=1)
    g = N.backprop(net, x, y)
    zs, acts = N.forward(net, x)
    h = 1e-6
    for j in range(4):   # nudge hidden neuron j's activation and run the rest of the network
        def cost_with(delta):
            a = acts[1].copy()
            a[0, j] += delta
            out = N._act("sigmoid", a @ net.layers[1].weight.T + net.layers[1].bias)
            return float(N.cost_each(net, out, y)[0])
        slope = (cost_with(h) - cost_with(-h)) / (2 * h)
        assert g["layers"][0]["dcost_da"][j] == pytest.approx(slope, abs=1e-6)
        assert g["layers"][0]["wanted_nudge"][j] == pytest.approx(-slope, abs=1e-6)


def test_the_three_factors_multiply_to_the_weights_gradient_and_softmax_says_when_they_do_not():
    net = N.init([3, 4, 2], "sigmoid", "squared_error", seed=2)
    x, y = _data([3, 4, 2], n=1)
    g = N.backprop(net, x, y)
    t = N.trace(net, x, y, [1, 2, 0])
    assert [s["weight"]["index"] for s in t["steps"]] == [[2, 1], [0, 2]]
    for s in t["steps"]:
        assert s["product_is_exact"]
        assert s["a_before"] * s["da_dz"] * s["dcost_da"] == pytest.approx(s["gradient"], abs=1e-12)
        j, k = s["weight"]["index"]
        assert s["gradient"] == pytest.approx(g["layers"][s["layer"]]["weight"][j, k], abs=1e-12)
        assert s["bias"]["gradient"] == pytest.approx(g["layers"][s["layer"]]["bias"][j], abs=1e-12)
    # The last layer's factors are the lessons' own: 2 * (a - y) for squared error.
    last = t["steps"][-1]
    assert last["dcost_da"] == pytest.approx(2 * (t["output"][0] - t["wanted"][0]))

    soft = N.init([3, 4, 3], ["sigmoid", "softmax"], "cross_entropy", seed=2)
    x, y = _data([3, 4, 3], n=1)
    t = N.trace(soft, x, y)
    assert t["steps"][-1]["product_is_exact"] is False and t["steps"][0]["product_is_exact"] is True
    g = N.backprop(soft, x, y)
    j, k = t["steps"][-1]["weight"]["index"]
    assert t["steps"][-1]["gradient"] == pytest.approx(g["layers"][-1]["weight"][j, k], abs=1e-12)


def test_the_paths_from_one_first_layer_weight_add_up_to_its_gradient():
    net = N.init([2, 3, 3, 2], "tanh", "squared_error", seed=4)
    x, y = _data([2, 3, 3, 2], n=1)
    total = sum(N.trace(net, x, y, [1, 2, m, o])["path_product"] for m in range(3) for o in range(2))
    t = N.trace(net, x, y, [1, 2, 0, 0])
    assert t["paths_from_first_weight"] == 6
    assert total == pytest.approx(t["first_weight_gradient"], abs=1e-12)


def test_a_step_lowers_the_cost_and_training_repeats_exactly_with_a_seed():
    x, y = _data([4, 6, 3], n=40)
    a, b = N.init([4, 6, 3], "sigmoid", "squared_error", seed=7), N.init([4, 6, 3], "sigmoid", "squared_error", seed=7)
    r = N.step(a.copy(), x, y, 0.5)
    assert r["cost_after"] < r["cost_before"]
    ca = [c for _, c in N.train(a, x, y, rate=0.5, steps=30, batch=8, seed=1)]
    cb = [c for _, c in N.train(b, x, y, rate=0.5, steps=30, batch=8, seed=1)]
    assert ca == cb and len(ca) == 30
    assert all(np.array_equal(p.weight, q.weight) for p, q in zip(a.layers, b.layers))
    assert N.cost(a, x, y) < N.cost(N.init([4, 6, 3], "sigmoid", "squared_error", seed=7), x, y)


def test_class_numbers_are_read_as_one_hot_rows_and_the_score_counts_right_answers():
    net = N.init([2, 3], "softmax", "cross_entropy", seed=0)
    x = np.array([[1.0, 0.0], [0.0, 1.0]])
    assert N.cost(net, x, np.array([0, 2])) == pytest.approx(N.cost(net, x, np.eye(3)[[0, 2]]))
    picked = N.predict(net, x).argmax(axis=1)
    assert N.score(net, x, picked) == 1.0


@pytest.mark.parametrize("make,words", [
    (lambda: N.init([2]), "at least an input size"),
    (lambda: N.init([2, 3, 1], ["softmax", "sigmoid"]), "only supported on the last layer"),
    (lambda: N.init([2, 3], "relu", "cross_entropy"), "probabilities"),
    (lambda: N.forward(N.init([2, 3]), [1.0, 2.0, 3.0]), "takes 2 numbers"),
    (lambda: N.trace(N.init([2, 3, 1]), [1.0, 2.0], [1.0], [0, 9, 0]), "not one of them"),
])
def test_what_it_refuses_it_refuses_in_a_sentence(make, words):
    with pytest.raises(N.WeightsError, match=words):
        make()
