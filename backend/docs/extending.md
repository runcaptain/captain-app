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

## Team views

A team view is a screen your team builds in code, in the project, without changing Captain: your own dashboard over
runs, a review queue, a view of your logs. Captain loads it from `captain/views/<name>/`, lists it under the
Explorer's Views (under "From <project>"), opens it in its own pane, and reloads it when a file changes.

### The folder

```
captain/views/<name>/
  mod.json      {"title": "Catalog rows", "place": "none", "icon": "table",
                 "overlay": {"width": 0.8, "height": 0.85}, "captain": {"view_api": 1}}
  main.js       an ordinary gpui-kit mod: export default class extends View { init(props, cx) {...} render() {...} }
  README.md
  .gitignore    kit/
```

- `title` names it in the Explorer. `icon` is one of the kit's icon names (`defaults/captain/kit/icons.js`, for
  example table, chart-line, database, flask-conical). `overlay` sizes its pane: up to 1 is a share of the window.
- `captain.view_api` is the view API it's written for. This Captain understands `1`. A view asking for more says
  "Update Captain" instead of loading.
- Don't commit a `kit/` folder: Captain puts its kit there when it loads the view, so a view always gets the kit of
  the Captain it runs in.

`cst view new <name>` scaffolds that folder from the template in `backend/core/captain_search/view_template/` (it shows a
dataset's rows in the result grid, filtered to the Explorer's pick).

### Checking it

`captain-app check captain/views/<name>` loads the view once with the kit, the way Captain will, and says exactly
what's wrong, naming your files. Run it before you push, and have an agent run it after every change. It never
writes into your folder.

### Loading and errors

Captain copies each view into its mods folder as `team-<name>`, with the kit as `./kit`, and copies it again when a
file changes. A changed view is checked first: if the check fails, it isn't copied, its last good version stays,
the Explorer shows it as "(last good version)", and opening it shows that version with the check's words. Other
views never notice. A view removed from the project goes away.

### What a view can use

From the kit (`./kit/...`), the same pieces Captain's own screens use:

| Module | What it gives you |
|---|---|
| `cst.js` | `api(path, {method, body, timeout, scoped})`: Captain's services, resolving `{ok, status, data, error, starting, failed, timedOut}`, never throwing. `watchScope(cx, fn)` and `watchWorkspace(cx, fn)` call you when the Explorer's pick or the space's workspace changes. `details()` for Copy details. |
| `ui.js` | `Card`, `Badge`, `Button`, `Tabs`, `Empty`, `ErrorState`, `Problem`, `Skeleton`, `Loading`, `Bars`, `Line`, `Table`, and `plainError(text, fallback)` so a raw key or path never reaches a person. |
| `grid.js` | `ResultGrid`: a query's rows with sort, filters, column stats, CSV download and TSV copy. |
| `chart.js`, `charts.js` | bars, lines and scatter from a table. |
| `lineage.js` | `LineageGraph`, the drawn lineage graph. |
| `tokens.js` | `palette(isDark(theme()))` (Captain's colors in light and dark), `FONT`, `MONO`, `RADIUS`. |
| `icons.js`, `brands.js` | `icon(name)` for `svg()`; `Brand(c, kind, size)` for a database's or cloud's mark. |
| `scopes.js` | the scope model's words and the Explorer's pick (`currentScope()`). |
| `scopemodel.js` | `ScopeModelPanel`: Project Structure, shown in Settings and in the `captain-scope-model` dialog. |
| `agent.js` | `askAgent(cx, prompt)`, `copyPrompt(cx, prompt)`, `openForm(task, args)`. |

From `"herdr"`: `theme()`, `shared`/`setShared`, `openOverlay`/`closeOverlay`, `notify`, `exec`, `readTextFile`,
`pickFolder` and the rest of the mod API (the app's `AGENTS.md`, "The `herdr` module").

### Which routes a view may call

Any route in [api.md](api.md), through `api()`, which adds the token, the space's workspace (`X-CST-Workspace`) and
the Explorer's pick. Prefer reading:

- the lists: `/api/datasets`, `/api/datasets/tree`, `/api/pipelines`, `/api/backtests`, `/api/runs`,
  `/api/investigations`, `/api/models`, `/api/connections`, `/api/grading`, `/api/signals`;
- a record and its parts: `/api/datasets/{name}/rows`, `/api/runs/{name}`, `/api/runs/{run}/queries`,
  `/api/lineage`, `/api/engines`, `/api/explorer`.

A view that writes (records a decision, starts a backtest) should do it through the same routes Captain's forms
use, after the person asks for it, and say what it did. A view never holds a secret: passwords go to
`POST /api/secrets` from Captain's own forms.

### The Explorer's pick

While the person has a scope, engine or project picked, every request a view makes carries
`X-CST-Scope: <kind>:<id>` (`scope`, `engine`, `project`, or `view`, which filters nothing), and the list routes
return only what belongs to it and below. So a view filters with no work of its own. Call `watchScope(cx, ...)` to
reload when the pick changes, and pass `api(path, {scoped: false})` for a request that must cover the whole
workspace.
