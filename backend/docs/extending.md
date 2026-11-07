# Extending Captain in your own code

Captain is a framework. Your team's search code lives in a `captain/` folder in your own repo, reviewed in pull
requests like any other code. Captain finds it, checks it, uses it in every run, and records exactly which commit of it
produced each result.

## Start

```sh
cst project init path/to/your/repo     # creates captain/ with an example metric and judge
cst project use path/to/your/repo/captain   # binds it to the current workspace (the app uses it too)
cst ext check                          # loads and tests every extension; exit 1 on any failure. Put this in CI
cst ext list                           # everything Captain can use, and where each one comes from
```

Captain finds the folder from `$CST_PROJECT`, then the folder bound with `cst project use`, then by walking up from
where you run `cst`.

## What you can add

| Folder | Decorator | Interface |
|---|---|---|
| `metrics/` | `@metric("name", definition=..., higher_is_better=True)` | `fn(ranked, grades) -> float`, for one query. Grades are 0 to 3 by product id |
| `judges/` | `@judge("name")` | `fn(query, product) -> grade` or `(grade, reason)`, grade 0 to 3 |
| `simulators/` | `@simulator("name")` | the same signature as the built-in `cascade-v1` |
| `stages/` | `@engine("name")` | a class with `__init__(spec, products)` and `search(query, k) -> [product ids]` |
| `compute/` | `@compute("name")` | a `ComputeProvider` subclass (`captain_search.compute_providers`): `plan`, `submit`, `status`, `logs`, `cancel`, `test`. It shows up as a kind in "Add a place to run work" |

Import the decorators from `captain_search.extensions`. Installed Python packages can add the same four kinds through
the entry point groups `captain_search.engines`, `.simulators`, `.judges` and `.metrics`, and `captain_search.compute`.

## How they're used

- **Metrics** are computed for every query in every backtest, next to nDCG, P@10, R@50 and MRR, and they appear in
  comparisons, segments and surfaces under their own names.
- **Judges** grade with `cst judge run --judge <name>`. Their grades are labeled as custom, with the judge's name and
  version.
- **Simulators** and **engines** are picked by name in a population or a pipeline spec.
- **Precedence:** your folder wins over installed packages, which win over Captain's built-ins. So you can replace a
  built-in on purpose.

## What a run records

Every backtest run carries a `manifest`. It holds Captain's version and commit, the Python version and platform, your
folder's path, git commit, uncommitted-changes flag and a hash of its files, and each extension the run used with its
version and source. A run made from uncommitted code says so.

## Trust

Extensions are code that runs with your permissions, like dbt models. Loading a folder runs its Python. Load only
folders you trust, and run anything else on a compute target, never in the app.
