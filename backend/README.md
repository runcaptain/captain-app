# Captain Search backend and `cst` CLI

The Python backend behind the Captain app, and the `cst` command line that agents and scripts use. The app bundles it
(see `scripts/package-captain-macos`), so app users install nothing else.

- `core/`: the `captain-search` package. Data model, versioned storage on S3, GCS, Azure or local disk, connectors,
  query generation, judges, shopper simulators, backtests, deploys, notebooks, investigations and the local service
  the app talks to.
- `runner/`: the container image that runs jobs on a team's own cloud.
- `docs/`: the API (`api.md`), the CLI (`cli.md`), the data model and how to extend Captain in your own code
  (`extending.md`).

## Develop

```sh
cd backend/core
uv venv && uv pip install -e '.[dev,warehouses]'
.venv/bin/python -m pytest -q
```

`backend/install.sh` puts `cst` in `~/.local/bin` for use outside the app.

Organization-specific data, such as store demos or a model registry, is never part of this code. `cst demo import`
reads it from a private file (`$CST_DEMOS`), and `cst demo seed` makes a synthetic store instead.

## Default sample workspace

`cst demo seed` loads **Sample North Pole**, a separate, synthetic workspace with
three 50-product catalogs, six lists, 30 queries and illustrative relevance labels.
Use the app's “Load Sample North Pole” task to load and select it. Remove it with
“Remove Sample North Pole” or `cst demo remove-sample`; added or edited work is
protected. See [sample-data.md](docs/sample-data.md) for schemas and safety rules.
