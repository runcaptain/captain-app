# cst command reference

Global flags: `-w/--workspace NAME`, `--json`, `--dry-run`.

## Built (steps 1 and 2)

| Command | What it does |
|---|---|
| `cst init NAME --storage URI [--org O]` | Create a workspace in a bucket, or attach to one that exists |
| `cst workspace list / use NAME / show` | Pick the workspace |
| `cst secret set NAME [--value V] / list / rm NAME` | OS keychain. Use `{secret:NAME}` (URL-quoted) or `{secret_raw:NAME}` in URLs |
| `cst sync` | Rebuild the local SQLite index from the bucket |
| `cst index` | Print INDEX.md, generated from the bucket |
| `cst conn add NAME --engine E --url U [--ssh user@host] [--runner URL] [--writable] [--row-limit N] [--replace]` | Shared connection, no secrets stored |
| `cst conn list / test NAME / rm NAME` | |
| `cst sql CONN -e SQL / -f FILE [--save NAME --kind K -m DESC] [--limit N] [--show N] [--local]` | Read-only SQL. `--save` snapshots the result as a dataset version with provenance |
| `cst data list [--kind K]` | Datasets and their latest versions |
| `cst data show / head / verify / pull REF` | REF is `name`, `name@v3`, `name@3` or `name@alias` |
| `cst data snapshot FILE --name N --kind K` | Version a local csv, tsv, parquet or jsonl file |
| `cst data alias REF ALIAS` | Move an alias with compare-and-swap |

## Evaluation, pipelines, backtests, deploys

| Command | What it does |
|---|---|
| `cst queries gen --catalog REF --name N [--classes ...] [--n] [--seed]` | Queries from the catalog by failure class |
| `cst queries seal REF --holdout 0.3` / `check REF` | Split and seal the holdout with a sha256 / check it |
| `cst judge run --queries REF:split --pipelines a@v1,b@v1 --judge llm|llm:MODEL|heuristic --into N` | Grade pooled top results, reusing earlier grades |
| `cst judge import FILE --name N` / `calibrate --llm REF --human REF` | Human labels / kappa and bias |
| `cst pipeline new N --engine E [--catalog REF]` / `publish FILE` / `list` / `show` / `diff A B` / `link N --registry S3URI` | Pipelines |
| `cst sim population create N --size 10000 --seed 1 [--click-judgments REF]` | Shopper population |
| `cst backtest plan N --arms base,challenger --queries REF:holdout --judgments REF --population REF --hypothesis "..."` | Pre-register |
| `cst backtest run N` / `report N` / `verify N` / `list` | Run, read, rerun-and-compare |
| `cst deploy env add N --tier demo|live --target record|webhook|captain` / `env list` | Environments |
| `cst deploy promote REF --env E [--override-reason ...] [--confirm]` / `rollback --env E` / `status` / `history` | Deploys |

## Notebooks, agent, daemon

| Command | What it does |
|---|---|
| `cst notebook new|list|open N` / `run N [-p k=v] [--save-rev]` | Notebooks; `run` is headless |
| `cst skill list|show|add` | Agent skills (6 built in) |
| `cst agent "question" [--yes]` | Ask the agent; it checks its figures against tool results |
| `cst serve [--port 0]` | The daemon (see api.md). Binding beyond localhost needs CST_ALLOW_REMOTE=1 and a TLS proxy |
| `cst app` | Open the desktop app |
| `cst docs [NAME]` | Print a guide that ships with cst, such as `cst docs trainer-contract`. Without a name: the list |
| `cst projects assist P` / `propose P --file proposal.yaml [--assist ID]` | A prompt for a coding agent to set up a project, and the proposal it files. A person accepts it in the app. An agent session can't accept, and can't change the project by hand while its proposal is open |
| `cst demo seed` | Load the separate Sample North Pole workspace (`--legacy`: fill this workspace with a labeled synthetic store) |
