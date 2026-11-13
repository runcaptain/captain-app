# Removed from the app, still in cst

On 2026-11-12 the owner walked the whole app and asked for far fewer screens. These left the screens. Their
backend code, routes and commands were left alone on purpose, so nothing a script or an agent relies on broke.
This list is here so a later cleanup knows what to look at. Nothing on it has been deleted from the backend.

| What left the screens | Still works from a terminal | Routes still served | Notes |
|---|---|---|---|
| Experiments (the `captain-investigations` screen, "New experiment", "Record a decision", "Attach evidence") | `cst investigate new, list, show, run, decide, prompt`, `CST_INVESTIGATION` | `/api/investigations`, `/api/signals` | The forms `investigation`, `decision` and `evidence` are still in `captain-forms/tasks.js`, hidden. Nothing on a screen opens them. `cst app open experiments` exits 1 and names the command |
| The "recommendations to review" count on the sidebar | `cst propose` | `/api/proposals` | A project's own proposals still show on its page |
| Compare, as its own screen (`captain-compare`) | `cst backtest report` | `/api/runs/<name>`, `/compatibility`, `/histogram`, `/queries`, `/api/compare-runs` | Not removed: it moved into Runs. Every route is still used |
| Launch review (the `handoff` slot in the layout, "Start a launch review", "Send to launch review") | `cst handoff` | `/api/handoffs` | The `captain-handoff` mod's files are still in the tree. The form `handoff` is hidden |
| Labels (the Labels tab of Data) | `cst labels define, preview, compute` | `/api/labels`, `/api/labels/<name>/preview` | Nothing on a screen asks for labels |
| The dataset folder tree, and renaming or removing a folder | `cst data mkdir, move, rename-folder, rmdir` | `/api/datasets/tree`, `/api/library/folders`, `/api/library/rename` | Folders are the groups of Data's Tables. Moving a dataset to a folder is on its page. `/api/library/rename` and `DELETE /api/library/folders` have no caller on a screen |
| The table search inside the old Tables tab | `cst tables search` | `/api/tables/search` | The list's search is `GET /api/data/tables?q=` now |
| The tour's steps "Open an experiment" and "Record a decision" | | `/api/onboarding` no longer lists them | The tour ends at an offline evaluation |
| The old evaluation form's fields | | | The form task `backtest` keeps its fields for a caller that hands every value over. The New menu and the palette open `captain-evaluation` |
| The engine page's Experiments tab | | `/api/investigations` is no longer asked for by that page | |

Asked for and not built, because a run can't do it yet. These are in `GET /api/evaluations/options` under
`not_yet`:

- which slices a run reports;
- which measures a run computes.
