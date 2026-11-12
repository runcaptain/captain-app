# `cst serve` API (contract between the daemon and the desktop app)

Base: `http://127.0.0.1:<port>`. Every request carries `Authorization: Bearer <token>`.
WebSockets pass the token as `?token=<token>`.

**Discovery.** `cst serve --port 0` prints exactly one JSON line on stdout as soon as it is listening:
`{"port": 51234, "token": "…", "pid": 123}`. It also writes the same object to
`$CST_HOME/serve.json` (default `~/.config/captain-search/serve.json`, mode 0600). The app spawns
`cst serve --port 0` (finding `cst` on PATH, then `~/.local/bin/cst`) and reads the first stdout line.
If `cst` is missing, the app shows: `uv tool install captain-search`.

All responses are JSON. Errors: HTTP 4xx/5xx with `{"error": "plain sentence"}`.

## Workspace

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, version}` |
| GET | `/api/workspace` | `{name, org, storage_uri, user}` |
| GET | `/api/lineage` | `{nodes:[{id, kind, label}], edges:[{src, dst, relation}]}` |

## Data

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/connections` | | `[{name, engine, network, writable}]` |
| POST | `/api/sql` | `{conn, sql, limit?, save?, kind?, description?}` | `{columns:[{name,type}], rows:[[…]], row_count, truncated, ms, dataset?}` (rows capped at 1000 in the response) |
| POST | `/api/sql/format` | `{sql, case: upper or lower, conn? or dialect?}` | `{sql}`, pretty-printed; identifiers and literals as written |
| POST | `/api/sql/transpile` | `{sql, to, conn? or from?}` | `{sql, from, to, warnings}` (what didn't carry over) |
| POST | `/api/sql/explain` | `{conn, sql}` | `{connection, plan, ms}`; EXPLAIN of a read passes the read-only guard, of a write is refused |
| GET | `/api/sql/history?conn=&status=ok or failed&source=app or notebook or preview or cli&q=&days=30&limit=200` | | queries run through Captain, newest first: `{id, connection, user, at, sql, tables, rows, ms, source, ok}`. Failed queries are logged too, and don't count as table usage |
| GET | `/api/snippets?engine=&q=` | | `[{id, title, description, sql, engine, golden, vars, created_by, created_at}]`, recommended (`golden`) first |
| POST | `/api/snippets` | `{title, sql, description?, engine?, golden?}` | the snippet; `vars` lists its `{{name}}` placeholders |
| PUT, DELETE | `/api/snippets/{id}` | any of the POST fields | the snippet, or `{deleted}` |
| GET | `/api/datasets` | `?kind=` | `[{name, latest, kind, rows, by, created}]` |
| GET | `/api/datasets/{name}` | `?v=` | DatasetVersion manifest |
| GET | `/api/datasets/{name}/head` | `?v=&n=50` | `{columns, rows}` |
| GET | `/api/datasets/{name}/insights` | | what uses it, from Captain's own records: `{used_by: {pipeline, run, environment, dataset: [{id, label, name, version, level, via, section, created_by, created_at}]}, notebooks: [{name, cells}], people: [{user, versions, builds}], activity: [{at, who, what, kind, id}], versions, versions_total, counts}`. `level` is how many steps downstream and `via` which of its versions. An unknown name is a 404 in words |

**Who reads a dataset.** Rows read through the app (`/rows`, `/head`), `cst.dataset("name")` in a notebook, and
`cst data head` / `pull` (an agent's when `CST_AGENT` is set) append one small record each to
`metastore/reads/<dataset>/<day>/` (data/reads.py). The same reader of the same version the same way counts once per
10 minutes, so paging isn't a read per page, and a failed write never fails the read. Insights' `readers` sums the
last 90 days: `{days, reads, by_via: {app, notebook, agent, cli}, readers: [{user, agent, reads, via, versions, last}]}`.

## Pipelines, backtests, deploys

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/pipelines` | | `[{name, latest, engine, repro_status, by, created}]` |
| GET | `/api/pipelines/{name}` | `?v=` | PipelineVersion manifest |
| GET | `/api/backtests` | | `[{name, latest, arms, primary_metric, runs}]` |
| GET | `/api/backtests/{name}` | | Backtest manifest + `runs:[{version, created, complete, independent}]` |
| POST | `/api/backtests/{name}/run` | `{}` | `{job}` |
| GET | `/api/runs/{name}` | `?v=` | BacktestRun manifest (see `entities.py`): `arms[].offline` (ndcg@10, p@10, r@50, mrr), `arms[].simulated` (ctr, conversion, rpv, p99_ms), `arms[].evidence`, `arms[].judged_at_10`, `comparisons[]` `{metric, a, b, delta, ci_low, ci_high, p, verdict}`, `independent`, `complete` |
| GET | `/api/jobs/{id}` | | `{id, status: queued|running|done|failed, progress 0..1, message, result?}` |
| GET | `/api/environments` | | `[{name, tier, target, current}]` |
| GET | `/api/deployments` | `?env=` | `[{id, environment, pipeline, action, created_by, created_at, override_reason, gate_results}]` |
| POST | `/api/deploy/promote` | `{pipeline, env, override_reason?, confirm}` | Deployment or `{error, gate_results}` with 409 |
| POST | `/api/deploy/rollback` | `{env, confirm}` | Deployment |

Evidence labels: `SIMULATED`, `LLM_GRADED`, `HUMAN_GRADED`, `REAL_TRAFFIC`. The UI shows one on every number.
Verdicts: `win`, `loss`, `noise` (gain under the MDE or CI crosses 0).

## Notebooks

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/notebooks` | | `[{name, latest, has_draft}]` |
| GET | `/api/notebooks/{name}` | | `{name, version, notebook}`: the draft if any, else the latest revision. `notebook` is nbformat 4 JSON |
| PUT | `/api/notebooks/{name}/draft` | `{notebook}` | `{ok}` (local autosave, not versioned) |
| POST | `/api/notebooks/{name}/save` | `{notebook, message?}` | `{version}` (write-once revision) |
| POST | `/api/notebooks/{name}/import` | `{notebook, message?}` (any nbformat 4 .ipynb) | `{name, version}` |
| POST | `/api/kernels` | `{notebook}` | `{session}` |
| WS | `/api/kernels/{session}` | | see below |

Cell metadata: `cell.metadata.captain = {kind: "sql"|"python"|"markdown"|"chart", conn?, var?, chart?}`.
A SQL cell's result is bound to the Python variable `var` (default `df_<n>`) inside the kernel.

Kernel socket, client to server: `{type:"execute", cell_id, kind:"sql"|"python", code, conn?, var?}` and `{type:"interrupt"}`.
`{type:"page", req, var, offset?, limit<=1000?, order?, desc?, where?, count?}` asks for a page of the DataFrame `var`,
sorted and filtered over all its rows (DuckDB, WHERE through the read-only guard). It answers
`{type:"page", cell_id: req, ...one row shape}` then `{type:"done", cell_id: req}`, or an error such as "df_1 isn't in
this notebook's Python any more. Run its cell again." A page sends no status messages.
Server to client: `{type:"status", state:"busy"|"idle"}`, `{type:"stream", cell_id, name:"stdout"|"stderr", text}`,
`{type:"table", cell_id, var, columns, rows, row_count, truncated}`, `{type:"display", cell_id, mime, data}`
(`text/plain`, `text/html`, `image/png` base64, `application/vnd.vegalite.v5+json`), `{type:"error", cell_id, ename, evalue, traceback}`,
`{type:"done", cell_id, ms}`.

## Agent

| Method | Path | Returns |
|---|---|---|
| GET | `/api/skills` | `[{name, version, description, tags}]` |
| WS | `/api/agent` | see below |

Client to server: `{type:"ask", text, context?:{notebook?, dataset?, run?}}`, `{type:"confirm", call_id, ok}`, `{type:"cancel"}`.

Server to client (one turn):
- `{type:"step", id, label, status:"running"|"done"|"failed"}`: a visible work step ("Reading your question", "Running SQL on warehouse").
- `{type:"tool_call", call_id, name, args, needs_confirm}`: when `needs_confirm`, the server waits for `confirm`.
- `{type:"tool_result", call_id, summary}`.
- `{type:"check_failed", attempt, problems:[{claim, expected, found}]}`: the draft reply stated a figure the tool results do not support.
- `{type:"corrected", attempt}`: the rewrite passed the check. The panel shows a "Checked and corrected" step with the problems.
- `{type:"repaired", problems}`: two rewrites failed, so the server fixed the figures itself.
- `{type:"text", text}`: the final reply (markdown), after checks.
- `{type:"done", ms, steps}` or `{type:"error", message}`.

---

# v2 additions (2026-11-06)

## Removed

- `WS /api/agent` and the built-in data-mining agent. The app hosts a **coding agent** (Claude Code or Codex) in a
  terminal instead. `GET /api/skills` stays: the skills are installed for the coding agent.

## Coding agent

| Method | Path | Returns |
|---|---|---|
| GET | `/api/coding-agent` | `{cwd, env:{CST_HOME, CST_WORKSPACE, PATH, CST_BIN}, agents:[{id:"claude"|"codex", label, command:[...], available}]}` |

The daemon prepares `cwd` (a per-workspace folder) with `AGENTS.md`, `CLAUDE.md` (how to use `cst`, the workspace,
the evidence rules) and `.claude/skills/<skill>/SKILL.md`. The app runs `command` in a PTY with that `cwd` and `env`
merged over its own environment, and shows it in an xterm panel. The agent drives everything through `cst ... --json`.

## Notebook formats

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/notebook-formats` | | `[{id, label, extensions:[".py"], import:true, export:true}]` |
| POST | `/api/notebooks/{name}/import` | `{filename, content}` (text, any supported format) or `{notebook}` (.ipynb JSON) | `{name, version, format, warnings:[...]}` |
| GET | `/api/notebooks/{name}/export` | `?format=<id>` | `{filename, content, mime}` |

Format ids: `ipynb`, `jupytext` (percent `.py`), `databricks` (`.py` source with `# COMMAND ----------`),
`marimo` (`.py`), `quarto` (`.qmd`), `rmarkdown` (`.Rmd`), `myst` (`.md`), `zeppelin` (`.zpln`). Format is detected
from the extension and content. SQL cells round-trip where the format has a SQL cell type (Databricks `%sql`,
Zeppelin `%sql`, Quarto/Rmd ```{sql}``` chunks, jupytext `%%sql` magics). Databricks and marimo have no place for
a SQL cell's connection, so it is kept as a first comment line: `-- captain: conn=demo-store var=zero`.
Chart cells export as Python. marimo's run-by-dependency order becomes top-to-bottom (a warning says so).

## Datasets as a directory

Dataset names may have up to 8 `/`-separated segments (`catalogs/acme/products`).

| Method | Path | Returns |
|---|---|---|
| GET | `/api/datasets/tree` | `{name:"", path:"", folders:[...same shape...], datasets:[{name, leaf, latest, kind, rows, created}]}` |

## Cloud profiles (per user, secrets in the OS keychain)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/cloud/providers` | | `[{provider:"aws"|"gcp"|"azure", auth:[{mode, label, fields:[{name, label, secret:bool, required:bool}]}]}]` |
| GET | `/api/cloud/profiles` | | `[{name, provider, auth, region, fields}]` (never secrets) |
| POST | `/api/cloud/profiles` | `{name, provider, auth, region?, fields:{...}, secrets:{...}}` | profile |
| POST | `/api/cloud/profiles/{name}/test` | | `{ok, identity?, error?}` |
| DELETE | `/api/cloud/profiles/{name}` | | `{ok}` |

## Workspaces (choose the storage backend)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/workspaces` | | `[{name, storage_uri, provider:"local"|"aws"|"gcp"|"azure", profile, current}]` |
| POST | `/api/workspaces` | `{name, storage_uri, profile?}` | workspace (creates, or attaches to an existing one) |
| POST | `/api/workspaces/{name}/use` | | `{current}` (the daemon switches) |

## Compute targets (shared in the workspace; they name a cloud profile)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/compute/kinds` | | `[{kind, provider, label, purposes:["backtest","serve"], fields:[{name,label,required,default?}]}]` |
| GET | `/api/compute` | | `[{name, kind, provider, profile, region, config, purposes}]` |
| POST | `/api/compute` | `{name, kind, profile?, region?, config}` | target |
| POST | `/api/compute/{name}/test` | | `{ok, detail}` |

Kinds: `local`, `modal`, `aws-batch`, `aws-ecs`, `aws-sagemaker`, `gcp-cloud-run`, `gcp-vertex`,
`azure-container-apps`, `azure-ml`, plus any a team adds (below). `/api/compute/kinds` items also carry
`live_tested` (the date that kind last ran a real job on a real account, or null) and
`capabilities:{logs, cancel}`.

## Compute providers and jobs

Every kind is a provider (`core/captain_search/compute_providers/`) with one interface:
`plan(job)` (the API calls, for `--dry-run`), `submit(job) -> handle`, `status(handle)`, `logs(handle, since)`,
`cancel(handle)`, `test()` and `describe()`. A job runs `cst <args>` once in the runner image (`runner/Dockerfile`,
entrypoint `cst`). It gets `CST_STORAGE`, `CST_WORKSPACE`, `CST_USER`, `CST_JOB_ID` and `CST_COMPUTE`, attaches to
that workspace in its bucket and writes results there, so cloud kinds need an `s3://`, `gs://` or `az://` workspace.

Each submitted job is recorded at `jobs/<id>.yaml` in the workspace, so teammates see it too:
`{id, kind, label, argv, target, target_kind, region, state, reason, handle, submitted_by, submitted_at, updated_at,
started_at, finished_at, exit_code, run_seconds?, backtest?, cancelled_by?}`. `state` is one of `queued`, `running`,
`succeeded`, `failed`, `stopped`, and `reason` is a sentence a person reads ("cst exited with code 2. The last log
lines say why."). Reading an unfinished job asks its provider and saves the answer.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/jobs?refresh=false&limit=200` | | job records, newest first. `refresh=true` asks each unfinished job's provider first |
| GET | `/api/jobs/{id}` | | a job this daemon is running (`{id, status, progress, log}`, as before), else the compute job record, refreshed |
| GET | `/api/jobs/{id}/logs?since=<cursor>` | | `{job, lines:[{ts, message}], next, where?, note?}`. Pass `next` back as `since` for only the newer lines |
| POST | `/api/jobs/{id}/cancel` | | the record, with `cancelled_by`. A finished job comes back unchanged with `note` |

`POST /api/backtests/{name}/run` with a cloud `compute` returns `{job}` for the daemon's own progress; its `result`
holds `job`, the compute job id to follow with the routes above.

CLI: `cst job list [--refresh]`, `cst job show <id> [--wait]`, `cst job logs <id> [--since c] [--follow]`,
`cst job cancel <id>`, and `cst job run <target> -- <cst args>` to run any command once on a target.
`cst backtest run <name> --compute <target>` and `cst pipeline retrain <name> --compute <target>` submit and record
a job the same way.

Where each provider reads state and logs:

| Kind | State from | Logs from | Cancel |
|---|---|---|---|
| `local` | the child process and its exit file | a file under `$CST_HOME/jobs/` | SIGTERM to the process group |
| `aws-ecs` | `ecs:DescribeTasks` (lastStatus, stopCode, exit code) | CloudWatch, the task definition's `awslogs` stream | `ecs:StopTask` |
| `aws-batch` | `batch:DescribeJobs` | CloudWatch, the job's log stream | `batch:TerminateJob` |
| `gcp-cloud-run` | the Execution (`ExecutionsClient.get_execution`) | Cloud Logging, `cloud_run_job` by execution name | `cancel_execution` |
| `gcp-vertex` | `get_custom_job` state | Cloud Logging, `ml_job` by job id | `cancel_custom_job` |
| `azure-container-apps` | `job_execution` status | Log Analytics (set `log_analytics_workspace_id`) | `begin_stop_execution` |
| `azure-ml` | `jobs.get` status | the job's `user_logs` | `jobs.begin_cancel` |
| `modal` | `FunctionCall.get(timeout=0)` (cannot tell queued from running) | `FunctionCall.logs.fetch` (the call's own lines) | `FunctionCall.cancel` |

Tested on a real account (2026-11-06): `local`, `aws-ecs` and `aws-batch` (Captain's AWS account, us-west-1, Batch on
a Fargate compute environment).
Also tested (2026-11-10): `azure-container-apps` and `azure-ml`, with the workspace in Azure Blob.
The others pass
the same contract tests against fakes of their SDKs (`core/tests/test_compute_providers.py`) and have not run
against a real account. What each needs before it can, and the test to run: [compute-live-testing.md](compute-live-testing.md).

### Adding a provider

A team adds a kind with an entry point in the `captain_search.compute` group, named by the kind:

```toml
[project.entry-points."captain_search.compute"]
slurm = "acme_captain.slurm:SlurmProvider"
```

`SlurmProvider` subclasses `captain_search.compute_providers.ComputeProvider`: set `kind`, `provider` (the cloud
login it needs: `aws`, `gcp`, `azure`, or anything else for none), `label`, `purposes` and `fields`, and implement
the methods above. `status` returns `compute_providers.status(state, reason, started_at=, finished_at=, exit_code=)`.
A plugin that fails to load, lacks a method or reuses a built-in name is skipped, and
`compute_providers.plugin_errors()` says why.

### Set up ECS for me

`cst compute setup serving <aws|modal|gcp|azure> [--set name=value ...]` sets up what a staged model needs on one
host, the same way for each: what it creates and costs, then a confirm (`--options`, `--policy`, `--leftovers`,
`--remove`). `GET /api/serving/hosts` lists the hosts as cards, and `GET|POST|DELETE /api/serving/setup` take
`cloud=<host>` (default `aws`). `cst docs staging` has each host, with what its first real run measured.

`cst compute setup aws-ecs --profile <p> --region <r> [--name ecs] [--storage s3://...] [--dry-run]` makes, or
reuses, everything an ECS target needs, prints each step, and adds the target to the open workspace:

- ECR repository `captain-search-runner` (push the image with `runner/build.sh --push <uri>:<version>`)
- CloudWatch log group `/captain-search/runner`, kept 30 days
- IAM roles `captain-search-ecs-execution` (pull the image, write logs) and `captain-search-ecs-task` (read, write
  and list the workspace prefix only, one inline policy per workspace)
- Fargate cluster `captain-search`, security group `captain-search-runner` (no inbound)
- task definition `captain-search-runner` (1 vCPU, 2 GB by default; a new revision only when something changed)

Everything is tagged `captain:purpose=cst-compute`. A same-named resource without that tag is left alone and
named in the output. Nothing runs between jobs. No NAT gateway is made: in a VPC without one, tasks get a public IP
(`assign_public_ip: ENABLED`) to reach ECR, S3 and CloudWatch, and the security group still blocks inbound traffic.

The AWS permissions it uses: `ecr:CreateRepository, DescribeRepositories, DescribeImages`;
`logs:CreateLogGroup, PutRetentionPolicy, DescribeLogGroups`; `iam:GetRole, CreateRole, ListRoleTags,
AttachRolePolicy, PutRolePolicy, PassRole`; `ecs:CreateCluster, DescribeClusters, RegisterTaskDefinition,
DescribeTaskDefinition, TagResource`; `ec2:DescribeVpcs, DescribeSubnets, DescribeNatGateways,
DescribeSecurityGroups, CreateSecurityGroup, CreateTags`. Running jobs uses `ecs:RunTask, DescribeTasks, StopTask,
TagResource`, `iam:PassRole` on the two roles, and `logs:GetLogEvents`.

## Building a backtest from the app

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/pipelines` | `{name, engine, catalog?, spec, description?}` | PipelineVersion |
| GET | `/api/engine-templates` | | `[{engine, label, template:{...spec}, needs_catalog}]` |
| POST | `/api/queries/generate` | `{catalog, name, classes?, n, seed}` | DatasetVersion |
| POST | `/api/queries/seal` | `{ref, holdout}` | DatasetVersion |
| POST | `/api/judgments/run` | `{queries, pipelines:[...], judge:"heuristic"|"llm"|"llm:<model>", into, depth}` | `{job}` |
| POST | `/api/judgments/from-logs` | `{logs, into, min_impressions?}` | DatasetVersion (click-derived grades) |
| GET | `/api/populations` | | `[{name, latest, simulator, size, seed, from_logs, click_model_judgments}]` |
| POST | `/api/populations` | `{name, simulator?, size, seed, click_judgments?, from_logs?}` | ShopperPopulation |
| POST | `/api/backtests` | `{name, mode:"simulated"|"historic", arms:[...], queries?, judgments?, population?, logs?, hypothesis, primary, mde, compute?}` | Backtest |
| POST | `/api/backtests/{name}/run` | `{compute?}` | `{job}` |
| GET | `/api/jobs/{id}` | | adds `log:[lines]` and `compute` (see "Compute providers and jobs" for cloud jobs) |

**Historic mode** replays real search logs. `logs` is an `events` dataset with columns
`query, product_id, position, clicked, purchased` (optional `session_id, ts, price`). The run:
1. builds grades from the logs, corrected for position bias (clicks over expected clicks at that rank);
2. takes the query mix and frequencies from the logs;
3. scores each arm with nDCG@10 against those grades, plain and traffic-weighted (`ndcg@10_tw`);
4. simulates shoppers who replay the historic query mix, with click rates by rank calibrated from the logs.

Its offline evidence is `LOGGED_CLICKS`, or `SIMULATED` when the logs dataset is tagged `synthetic`. Evidence order,
weakest first: `SIMULATED`, `LLM_GRADED`, `LOGGED_CLICKS`, `HUMAN_GRADED`, `REAL_TRAFFIC`.
Runs gain `mode` and `arms[].offline["ndcg@10_tw"]` in historic mode, and `arms[].offline["coverage"]` (share of
the arm's top 10 that has a logged grade).

## Environments with compute

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/environments` | `{name, tier, compute?, target:{type, ...}, gates?}` | Environment |

`GET /api/environments` items gain `compute`, `region`, `endpoint`. Target types: `record`, `webhook`, `captain`,
`sagemaker-endpoint`, `ecs-service`, `cloud-run`, `container-app`, `azure-ml-endpoint`.
A promote to a cloud target returns `gate_results.target.plan` (the exact API calls) and runs them unless
`dry_run:true` is in the body.

## Model registry and lineage

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/api/registry/import` | `{uri?:"s3://<bucket>", profile?}` (default: $CST_MODEL_REGISTRY or the demos file) | `{job}` (read-only on the registry) |
| GET | `/api/models` | | `[{id, brand, architecture, version, status_kind, status, repro_status, parent_label, parent_id, training_commit, dvc_alias, attached_to_demo, results:[{label,text}], known_issues:[...], path}]` |
| GET | `/api/models/{id}` | | the same plus `manifest` (raw) and `files:[{path, bytes}]` |
| POST | `/api/models/pull` | `{model, dest?, parts?, profile?}` | `{job}`: downloads a version, checked against its SHA256SUMS |
| POST | `/api/models/publish` | `{pipeline:"name@vN", arch, brand, registry?, profile?, status?:"candidate"}` | `{job}`, whose result is `{model:"brand@vN", architecture, brand, version, pipeline, path, files, bytes, repro_status:"not-tested", snapshot, warnings}` |
| POST | `/api/models/import-version` | `{pipeline, mlflow?:"name@version", mlflow_run?, registry_path?:"arch/brand/version", from?, description?}` | `{job}`, whose result is `{pipeline:"name@vN", source, metrics, parents}` |
| GET | `/api/lineage` | `?view=artifacts|models` | `{nodes:[{id, kind, label, status?, group?}], edges:[{src, dst, relation}]}` |

Publishing (`cst models publish NAME@vN --arch A --brand B [--registry URI]`) writes a new write-once
`<arch>/<brand>/vN/`. It claims the number with a create-only `.claim`, uploads the parts (`weights/` from the
version's `artifact/` files, `code/`, `env/`, `inputs/` as refs only, `results/`, `REPRODUCE.md`, `reproduce.sh`), then
`SHA256SUMS`, then `MANIFEST.yaml` last and create-only. Readers only list versions that have a manifest, so a publish
that stops partway leaves nothing visible, and its number stays used. Concurrent publishes get different numbers.
Files of 64 MB or more go up as a multipart upload that completes create-only. `repro_status` is `not-tested`.
A line is added to `INDEX.md` and the registry is imported again.

Importing a version (`cst models import-version PIPELINE --mlflow NAME@VER | --mlflow-run ID | --registry-path
A/B/V [--from REF]`) records a model trained elsewhere as a new pipeline version. Engine and spec come from `--from`
or the pipeline's latest version. It carries the metrics (`train_metrics`), params, `registry_ref` (the artifact
location), `parents` (`mlflow-model:NAME@VER`, `mlflow-run:ID` or `registry:BRAND@VER`) and `source`.

In the `artifacts` view, lineage comes from an index written when each version is published
(`lineage/edges/<ulid>.json`, write-once: `{from, to, relation, at, labels?}`). A workspace made before the index
has no `lineage/index.yaml`, so lineage is rebuilt by reading every manifest until `cst lineage reindex` backfills
it. A source that can't be found (deleted, or from another workspace) is a node of kind `unknown` labelled
"unknown source: <ref>". Relations: `produced_by`, `derived_from`, `candidates_from` (judgments from a pipeline's
results), `graded_in` (human grades from a grading queue), `parent`, `retrained_from`, `trained_on`,
`evaluated_on`, `deployed_from`.

`status_kind`: `current`, `candidate`, `rolled-back`, `unattached`, `other`. In the `models` view, node kinds are
`architecture`, `model`, `model-unregistered` (a parent the registry does not hold) and `demo`. Relations:
`built_on` (model to architecture), `parent` (parent to child), `attached` (model to the demo running it).

---

# v3 additions (2026-11-06)

## Commands: everything the CLI can do

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/commands` | | `[{path:["backtest","plan"], help, params:[{name, kind:"string"|"int"|"float"|"flag"|"choice", argument, opts, required, default, help, choices, multiple, secret}]}]` |
| POST | `/api/commands/run` | `{path, args:{<param name>: value}, dry_run?}` | `{command:"cst ...", exit_code, output (parsed --json), error}` |

Hidden from this list: `serve`, `app`, `code`, `notebook open`, `pipeline serve`.

## Workflows

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/workflows` | | `[{name, latest, steps, schedule, description, runs, last_status, last_run}]` |
| GET | `/api/workflows/templates` | | `[{id, description, params, steps, schedule?}]` |
| GET | `/api/workflows/{name}` | `?v=` | Workflow + `yaml` |
| POST | `/api/workflows` | `{name, steps:[{id, command, args, needs, position?}], params, schedule?, description?}` | Workflow (new version) |
| POST | `/api/workflows/{name}/run` | `{params?}` | `{job}`; job `log` gets one line per finished step |
| GET | `/api/workflows/{name}/runs` | | WorkflowRun list, newest first: `{status, trigger, steps:[{id, status, command, exit_code, error, ms, output}]}` |

Step args may contain `${params.<name>}` and `${steps.<id>.<field>}` (a field of that step's JSON output).
`schedule` is five cron fields in UTC; a running daemon fires due workflows.

## Datasets

`GET /api/datasets/{name}` adds `sample_schema:[{name,type}]` for by-reference datasets (read from the first
2 MB), or `sample_schema_error`. By-reference datasets list their files in `external:[{uri, bytes, etag,
sha256, mutable}]`.

## Dataset classes (team-defined, replaces fixed kinds in the UI)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/dataset-classes` | | `{classes:[{name, description, color, role, required_columns, datasets, created_by}], roles:["catalog","queries","judgments","events","logs"]}` |
| POST | `/api/dataset-classes` | `{name, description?, color?, role?, required_columns?}` | class |
| PUT | `/api/dataset-classes/{name}` | same fields | class |
| DELETE | `/api/dataset-classes/{name}` | | `{ok}`; 400 if any dataset still has it |
| PUT | `/api/datasets/{name}/classes` | `{classes:[...]}` | `{dataset, classes, role, warnings}` |

`GET /api/datasets?class=<name>` filters; list, tree and detail items carry `classes`, and detail carries `role`.
A class's optional `role` tells the tools what the data can feed (a catalog, a query set, grades, events).
A dataset takes at most one role. Classes without a role are plain labels. Five defaults are seeded on first
use and can be edited or deleted. Classification is mutable metadata with its own history; versions stay write-once.

## Cost against value

A platform team should score every model on build, upkeep and serving cost against what it adds, and retire
what doesn't pay. A cost is mutable metadata on a write-once pipeline version, kept with its history.

| Method | Path | Body / result |
|---|---|---|
| GET, PUT | `/api/pipelines/{name}/v/{n}/cost` | `{train_usd, train_gpu_hours, gpu_type, serve_usd_per_1k, note}` and `history` |
| GET | `/api/costs` | every version with cost, measured latency and `roi` from its latest backtest |
| GET, PUT | `/api/cost-settings` | `{queries_per_day}`, used to price serving per month |

Each `ArmResult.cost` holds `train_usd, serve_usd_per_1k, p50_ms, p99_ms, latency_source` (latency is measured per
query in the run). Primary comparisons carry `cost: {extra_serve_usd_per_1k, extra_train_usd, extra_monthly_usd,
gain_per_1k_usd, flag}`. Monthly extra cost is extra serving at `queries_per_day` x 30, plus extra training spread
over 12 months. The flag is `retire` (costs more, no measured win), `worth-it`, `cheaper-and-better` or `unproven`.
`BacktestRun.wall_s` is the run's wall time. CLI: `cst pipeline cost set|show`, `cst cost ledger`, `cst cost settings`.

## What a run costs on a cloud (cost_estimates.py)

Not the ledger above (which scores a model version's cost against its gain): this is what one piece of work costs
where it runs, before and after.

| Route | What |
|---|---|
| `POST /api/costs/estimate` `{kind, where?, size?}` | `{low, high, currency, unit: run\|hour, basis: [sentences], priced_as_of, price_source: live\|bundled, confidence: rough\|measured, free, available, text, confirm, threshold, minutes_low, minutes_high, machine, hourly}`. `low`/`high` are null and `text` says why where there is no estimate. |
| `GET /api/costs/runs?project=` | recorded runs: `{id, kind, where, size, units, estimate, actual}` |
| `GET /api/projects/{id}/costs` | `{usd, runs, text, recent}`: the project's running total on the cloud. Settles finished training jobs first. |
| `PUT /api/settings/costs` `{confirm_above_usd}` | the estimate above which a form asks again (default 5) |
| `POST /api/projects/{id}/starter/train` `{compute?}` | with a compute target: `{job, live, pending: true, estimate}` |

- Kinds: `training` (`size.how`: `starter` or `command`, `rows`), `evaluation` (`queries`, `arms`), `synthetic-events`
  (`sessions`), `snapshot` (`where` is a connection, `size.sql`), `judging` (`where` is a model, `pairs` or `queries`,
  `depth`, `arms`), `staging` (per hour; `instances`, `instance_type`), `workflow`.
- Prices: AWS only (the provider with live-tested jobs). Read from the AWS Price List API with the target's own
  login, cached six hours. Without that permission, `price_table.py` answers and every text says its date. Other
  providers answer "No estimate for <provider> yet."
- Time: a rough model per kind (`ROUGH`), replaced by this workspace's own earlier runs of the same kind once there
  are any on a cloud (`confidence: measured`).
- Actuals: when a compute job reaches a final state, `jobs.refresh` adds `cost` to its record (`{usd, seconds,
  billed_seconds, machine, hourly_usd, priced_as_of, text, provider_usd}`) and to `costs/runs/<job id>.yaml`. On ECS
  the clock starts at the image pull, as Fargate bills. `provider_usd` stays null: AWS reports no cost per task.
- Checked on a real account (2026-11-10, us-west-1, ECS Fargate, 1 vCPU and 2 GB): the demo store's starter ranker
  trained as a job. Estimate: $0.0009 to $0.0013, 60 to 82 s. Actual: 50 s from image pull to stop, billed as 60 s,
  $0.0009. The candidate, its cost, the history line, the project total and the live run (251 events through the
  bucket) all landed. The data is simulated and it is one run, so read it as "the path works", not as a benchmark.
- CLI: `cst costs estimate|runs|project|confirm-above`, `cst projects starter-train <p> --compute <target> [--wait]`.

## Monitor, retrain and gate

`cst monitor record --env E --logs L [--since D] [--where SQL]` aggregates an events dataset in DuckDB where it
lives and appends a snapshot for the deployed pipeline: searches, impressions, ctr, conversion, mrr_click,
clicks_at_3 (LOGGED_CLICKS). `GET /api/monitor/{env}` returns `{env, snapshots}`. `cst monitor check` compares the
latest snapshot with the pipeline's first, best or previous one and returns `degraded`.

`cst pipeline retrain NAME[@vN]` runs the command the pipeline declares in `spec.train.command` and publishes a new
version whose parent is the old one. The command gets `CST_TRAIN_INPUTS`, `CST_PARAMS` and `CST_OUT`, and writes
`CST_OUT/result.yaml` (see [trainer-contract.md](../core/captain_search/docs/trainer-contract.md), or `cst docs trainer-contract`). The older `$CST_TRAIN_OUT` file still works.
Running it locally needs `--local-exec`. On a compute target it runs as a job in the runner image and returns
`{pending:true, job, live_id, parent}` with no `pipeline` ref. `--wait` returns the new version once the job records it.
`POST /api/pipelines/{name}/retrain` takes `{compute?, inputs?, params?, as_of?}`.

`PipelineVersion` carries `train_inputs:[{ref, kind, role, uri, label_source, as_of}]`, `train_metrics`,
`train_params`, `parents:[ref]` and `source:{kind: retrain|mlflow|registry, ...}`. They are empty on older versions.

Workflow steps take an optional `when`. A step whose `when` resolves to "", false, 0, none, null or no is skipped
with `reason: "condition false"`, and so is every step that needs it. Skipped results have no `command`. The
`retrain-and-review` template records, checks, retrains when degraded, and backtests the new version against what
is live. It stops at a candidate ready for review. Captain never promotes: production promotion is the customer's
own deployment process (see Handoff).

## Segments and surfaces

`BacktestRun.segments` breaks nDCG@10 down by query `class` and, when queries carry weights, by traffic band (head
= top 50% of searches, torso = next 30%, tail = last 20%). Each row: `{kind, segment, arm, n, ndcg@10, judged@10,
delta, ci_low, ci_high, verdict}`. Segments under 5 queries say `too few`.

`Backtest.surfaces` lists extra places the pipeline serves: `{name, queries, judgments, guardrails: [{metric, min,
max_drop}]}`. CLI: `--surface name=related,queries=q@v1:holdout,judgments=j@v1 --guardrail "related ndcg@10
max_drop 0.01"`. `BacktestRun.surfaces` holds per-surface arm metrics, comparisons and guardrail results. A live
promote adds the gate `surface_guardrails`, which fails when any guardrail fails.

## Experiment proposals

An agent or a person proposes, and a person approves before anything runs. Agent sessions set `CST_AGENT=<name>`
(or pass `--agent`). They can propose, but approving or rejecting is refused in an agent session.

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/proposals?status=` | newest first: `{id, title, hypothesis, rationale, arms, queries, judgments, population, primary, mde, author, agent, status, review, backtest, run, result, history}` |
| GET | `/api/proposals/{id}` | one proposal |
| POST | `/api/proposals/{id}/review` | `{decision: approved or rejected, note}` |
| POST | `/api/proposals/{id}/run` | `{job}`. Plans and runs the backtest. Approved only |

Status: proposed, then approved or rejected, then ran. A live promote of a pipeline introduced by an agent
proposal adds the gate `human_approval`. CLI: `cst recommend new|list|show|approve|reject|run` (`cst propose` still works). The app lists them as recommendations, with the agent's icon. Without `--agent` or `CST_AGENT`, the CLI recognises Claude Code, Codex and Cursor from the shell they run it in. `cst serve` never does: requests from the app are a person's.

## Human grading

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/grading` | queues with status |
| GET | `/api/grading/{name}` | `{pairs, graded, share, graders: {who: n}, agreement_pairs, kappa, published}` |
| GET | `/api/grading/{name}/next` | `{done, remaining, pair: {id, query_id, query, product_id, first_seen, overlap}, product: {title, brand, ...}, scale}` |
| POST | `/api/grading` | `{name, queries, pipelines, depth?, sample?, overlap?}` starts a queue |
| POST | `/api/grading/{name}/grade` | `{pair, grade: 0-3, note}` |
| POST | `/api/grading/{name}/publish` | `{into}`. Publishes a judgments dataset with judgment_source human (HUMAN_GRADED) |

A queue holds the deduplicated top-k pairs of the given pipelines, shuffled, with an overlap share every grader sees.
Kappa shows once 10 or more pairs have two grades. CLI: `cst grade queue|list|next|set|status|publish`.

## Labels as code

`cst label define NAME --sql "max(case when clicked and position <= 3 then 1 else 0 end)" --grain search` publishes a
versioned label (grain: search, session, user or impression). `preview` and `compute` run it in DuckDB over an
events dataset where it lives. `compute` writes one row per group, with provenance pinning the label and logs
versions. `GET /api/labels` lists them, and `GET /api/labels/{name}/preview?logs=&v=&limit=` previews.

## Replay over randomized logs

`cst replay sweep NAME --logs L --score S --action A --outcome Y [--propensity P] [--direct Y]` sweeps "treat when
score >= t" and publishes a `replay` with `curve: [{threshold, trigger_rate, n, matched, outcomes: {y: {ips, se,
snips, delta_vs_never, kind: direct|directional}}}]`. Evidence is LOGGED_CLICKS, or SIMULATED on synthetic logs.
`GET /api/replays` lists them.

## Benchmarks

`cst bench rows DATASET` times the first page with a count, pages at 25%, 50% and 75%, the last page, and a sorted
page, and keeps the result. `GET /api/bench` returns `[{id, dataset, rows, bytes, where, machine, timings: [{step,
offset, ms}], worst_ms, at, by}]`.

## Investigations, signals and decisions (spec S1, S3, S6, S7)

The investigation is the main object. State: draft, running, ready_for_review, decided. Agents (CST_AGENT) can open,
revise, attach evidence and run within the execution policy. Only a person records the decision or changes the policy.

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/investigations?state=&q=` | list: `{id, title, state, hypothesis, initiator, revision, runs, latest_run, decision, updated_at, missing}` |
| POST | `/api/investigations` | `{title, hypothesis?, baseline?, candidates?, queries?, judgments?, population?, primary_metric?, mde?, guardrails?, agent?}` |
| GET | `/api/investigations/{id}` | `{..., revisions, evidence, runs: [{run, rev, status, error, idempotency_key, by, agent, at}], signals, decision, history, plan, missing}` |
| POST | `/api/investigations/{id}/revise` | plan fields plus `{hypothesis, note, expected_revision}`. 409 if expected_revision is stale |
| POST | `/api/investigations/{id}/evidence` | `{kind: dataset or run or slice or query or signal or note or label or replay or handoff, ref, note}` |
| POST | `/api/investigations/{id}/run` | `{idempotency_key?}` returns `{job}`. Pins the current revision. Failed runs stay listed |
| POST | `/api/investigations/{id}/decide` | `{outcome: continue or online-test or reject or inconclusive, claim, interpretation, limitations, next_step, runs?, slices?}` |
| GET | `/api/investigations/{id}/export` | `captain.investigation/v1`: investigation, decision, runs, signals |
| GET | `/api/investigations/{id}/prompt?ask=investigate or run or explain or panel` | `{prompt}`, built from real context, unknowns marked, no secrets |
| GET | `/api/decisions/{id}` | the write-once decision |
| GET, POST | `/api/signals` | candidate signals: `{name, source, definition, rationale, available_at, investigation?}` |
| POST | `/api/signals/{id}/check` | `{what: availability or leakage, status: ok or problem or unchecked, note}` (a note is required) |
| GET | `/api/execution-policy` | `{agent_runs, max_queries, max_shoppers, compute, max_runs_per_day}` |

A claim of `online-lift` is refused unless the runs carry REAL_TRAFFIC evidence. Offline decisions get the limitation
"not proof of online or profit lift" added automatically. CLI: `cst investigate new|list|show|revise|evidence|run|decide|export|prompt|policy`,
`cst signal new|list|check`.

## Handoff (Captain stays outside production)

A promote to a live environment is a handoff. It runs the readiness checks, writes a candidate bundle and deploys
nothing. `GET /api/handoffs`, `GET /api/handoffs/{id}`, `POST /api/handoffs/{id}/link {system, deployment_id, url, live}`
(this is what moves a live environment's current version), `POST /api/handoffs/{id}/outcome {metric, value, source,
experiment, ci_low, ci_high}` (REAL_TRAFFIC). Live rollbacks are refused. Demo-tier localhost environments still promote.

## Onboarding

`GET /api/onboarding` returns `{cst, workspace: {name, storage, kind}, workspaces, counts, steps: [{id, title, done,
optional?, detail, command}], next}`. The steps are workspace, cloud, data, registry, pipeline, run and agent. Each
is read from real records, so onboarding never marks a step done that isn't. The tour ends at an offline
evaluation: experiments have no screen since 2026-11-12, so it no longer asks for one or for a recorded decision.

## Inspect differences (spec S5), built for native screens

Lists are sorted, filtered and paged on the server, because native mods must not hold or sort whole result sets.

| Method | Path | Result |
|---|---|---|
| GET | `/api/runs/{run}/queries?a=&b=&sort=delta or query or ndcg_a or ndcg_b or weight or overlap&desc=&offset=&limit=&class=&q=&only=regressions or improvements or unchanged` | `{total, total_unfiltered, counts, rows: [{query_id, query, class, weight, ndcg_a, ndcg_b, delta, judged_a, judged_b, overlap10}]}` |
| GET | `/api/runs/{run}/queries/{query_id}?a=&b=&k=20` | both arms' ranked lists: `{a: {arm, ndcg10, judged10, items: [{rank, product_id, title, brand, price, grade (null = unjudged), other_rank, moved}]}, b: {...}, dropped, new, grade_scale, notes}` |
| GET | `/api/runs/{run}/compatibility` | `{trustworthy, warnings: [{code, text}]}`: not-independent, incomplete, uneven-coverage, exposure-bias, offline, not-holdout, small-slices |
| GET | `/api/runs/{run}/histogram?bins=21` | the per-query delta distribution, pre-binned: `{n, edges, counts}` |
| GET | `/api/compare-runs?a=&b=` | `{comparable, issues: [{code, text}]}` across two runs |

## Regression testing (the Runs screen's Compare)

`GET /api/regression?model=&project=&limit=` returns `{rows, models}`: every evaluation run, newest first, one row
each. A row is `{name, version, created, mode, model, against, metric, delta, ci_low, ci_high, comparison, evidence,
value, against_value, too_few_judged, own_labeler, independent}`. The model judged is the primary comparison's B arm,
and `value` and `against_value` are the two arms' scores on that measure. `comparison` is `win`, `noise` or `loss`
as the run recorded it; the screens say Better, No clear difference and Worse. `too_few_judged` (an arm has under 90%
of its top 10 judged) and `own_labeler` (the model learned from the labeler that grades the test set) come with every
row, because either one changes what the result means. `model` keeps one model's rows, by its name without the
version. `models` lists every model that has a row. `?project=` and `X-CST-Scope` filter by evaluation.

`GET /api/compare-runs?a=<name@vN>&b=<name@vN>` (already there) says whether two runs' numbers are on one scale:
`{comparable, issues: [{code, text}]}`.

## Per-request workspace

Every route accepts `X-CST-Workspace: <name>` (or `?workspace=<name>`), so one daemon serves all workspaces. A Captain
space bound to a workspace sends it, and switching spaces switches what the screens read. Without it, the daemon's
current workspace is used. An unknown name returns 400 with the known names.

## Cloud summary

`GET /api/cloud/summary` returns which cloud the workspace is on, without calling the cloud: `{workspace, storage,
storage_provider, storage_provider_label, storage_profile, active: {name, provider, label, auth, region, account_hint,
last_test: {ok, identity, error, at}}, profiles: [...], compute: [{name, kind, provider, profile, region, purposes}],
can_run_in_cloud, notes}`. `POST /api/cloud/profiles/{name}/test` (or `cst cloud test`) checks the login and saves
`last_test`.

## Secrets

`POST /api/secrets {name, value}` stores a value in the OS keychain in the daemon's own process, so a password typed
into the app never reaches a command line or process list. The value is never returned. `GET /api/secrets` lists
names only. `DELETE /api/secrets/{name}` removes one. Connections reference secrets as `{secret:NAME}`.

## Connection kinds

`GET /api/connections/kinds` lists every database the app can connect to, with the form's fields (name, label, type:
text, number, secret, path or choice, required, default, help, placeholder, choices), the driver, a docs link, and
whether the driver is installed. Kinds: Snowflake, Databricks, BigQuery, Redshift, PostgreSQL, MySQL, Trino or Presto,
Athena, ClickHouse, SQL Server, DuckDB, SQLite. `POST /api/connections {name, kind, fields, writable?, replace?}`
builds the connection. Secret fields are refused there: store each first with `POST /api/secrets` under
`<NAME>_<FIELD>` (the response lists them). `POST /api/connections/{name}/test` runs `select 1` and returns `{ok,
ms}` or `{ok: false, error}` in plain words. Install every driver with `pip install 'captain-search[warehouses]'`.

## Extensions (plan 0)

`GET /api/extensions` returns `{extensions, errors, project}`.
- `extensions`: every engine, simulator, judge and metric the workspace can use, each with kind, name, version, source
  (`builtin`, `package:<dist>` or `project:<file>`), description and determinism, plus metrics' `higher_is_better`
  and `definition`.
- `errors`: what failed to load or failed its check, in plain words.
- `project`: the `captain/` folder with its git commit, uncommitted-changes flag and file hash.

Backtest runs now carry `manifest` (see docs/extending.md).

## Your own data: storage, dropped files, folders, model registries

- `POST /api/storage/check {uri, profile?}` returns `{ok, can_read, can_write, existing_workspace, error}`. Onboarding
  calls it before `POST /api/workspaces`. It writes one marker (`.captain/access-check`) and deletes nothing. Errors are
  plain sentences ("This login isn't allowed to write to s3://...").
- `POST /api/datasets/import {paths: [files or folders], folder}` returns `{job}`. Its result is `{imported, updated,
  unchanged, skipped: [{path, reason}]}`. Supported: csv, tsv, parquet, json, jsonl, ndjson, up to 2 GB a file.
  Dropped folders keep their structure. A file with an existing name becomes a new version, and an unchanged one is
  skipped. The kind (catalog, queries, judgments, events, generic) is guessed from the columns.
- Folders are organization only. Moving copies nothing and changes no version.
  - `GET /api/datasets/tree` returns the folder tree as the person arranged it.
  - `POST /api/library/folders {path}` makes a folder, and `DELETE /api/library/folders?path=` removes an empty one.
  - `POST /api/library/move {names, folder}` moves datasets.
  - `POST /api/library/rename {from, to}` renames a folder.
- Model registries (lineage):
  - `GET /api/registries` lists the attached ones.
  - `POST /api/registries {uri, profile?, label?}` returns `{job}`. It attaches a registry and reads it, or re-reads it
    if already attached.
  - `DELETE /api/registries?uri=` detaches one.
  - The address can be `s3://`, `gs://`, `az://` or a local folder. Leave out the scheme and Captain uses the cloud
    of the workspace's storage, else the only connected cloud, else AWS. Leave out the login and Captain uses the
    workspace's own login for that cloud, else the only one, else the last that passed its test.
  - The layout is `<architecture>/<brand>/<version>/MANIFEST.yaml` under any prefix. `/api/models` and
    `/api/lineage?view=models` cover every attached registry.

CLI: `cst data import <paths> --folder <f>`, `cst data mkdir|move|rename-folder|rmdir`, `cst registry attach|list|detach`.

## Table browser (Querybook's table explorer)

- `GET /api/tables` lists each connection with what's been read of it: `{connection, engine, pulled_at, schemas, tables}`.
- `POST /api/tables/{conn}/pull {schemas?}` returns `{job}`. It reads schemas, tables, views, columns, types and
  comments through SQLAlchemy's inspector, falling back to `information_schema` where a driver can't reflect. The
  result is cached in the workspace as `metastore/<conn>.json`. Read-only on the database.
- `GET /api/tables/{conn}` returns the cached schemas and tables. It's a 404, with a sentence, until the first pull.
- `GET /api/tables/{conn}/{schema}/{table}` returns columns, comment and `pulled_at`, plus `insights`:
  `{queries, frequent_users, queried_with, examples}`.
- `GET /api/tables/{conn}/{schema}/{table}/sample?n=&where=&order=&desc=` returns a few rows through the read-only guard.
- `GET /api/tables/search?q=&conn=` searches table names, comments and column names. Name matches rank first.
- Insights come from a log of every query run through Captain (the app's SQL, notebook SQL cells and `cst sql`). The
  log is append-only under `metastore/queries/<day>/`. The tables each query reads are found by parsing it with
  SQLGlot.

CLI: `cst tables pull|list|show|search|sample`.

## Every table in one list (the Data screen's Tables)

`GET /api/data/tables?q=&kind=&project=` returns `{rows, connections}`: the datasets Captain keeps versions of and
each connected database's tables, together, sorted by name. A dataset is a table Captain keeps versions of.

- A dataset row: `{kind: "dataset", name, where, folder, rows, versions, version, last_read, columns, classes,
  dataset_kind, description, by_reference}`. `versions` is how many Captain keeps. `where` is in words:
  "Captain, folder logs", "Captain, a saved query on demo-store". `last_read` is the last day anything read it.
- A database table row: `{kind: "table", name, where, connection, schema, engine, rows, versions: null, last_read,
  columns, type, description}`. Captain keeps no versions of it. `last_read` is when its database's tables were
  last read.
- `q`: every word must be in the name, where it lives, the description or a column's name. A row matched only
  through a column carries `matched_column`. `kind` is `dataset` or `table`.
- `?project=` and `X-CST-Scope` filter the datasets. A database's tables belong to no project, so they are left
  out while something is picked. `connections` always lists every database: `{connection, engine, pulled_at,
  tables}`.
- It asks no database anything. Database tables come from what `POST /api/tables/{conn}/pull` cached, and
  `last_read` from one listing of the read log's keys.

## Projects (one per company)

A project groups one company's connections, datasets, pipelines, models, backtests, investigations and registries
inside a workspace. Membership is organization only, and nothing moves or is copied. An item is in a project when:
- it was assigned there (`POST /api/projects/assign {project, items: [{kind, name}]}`; project null takes items out);
- a registry brand maps to the project (`brands`, which default to the project's id). This claims its models and the
  pipelines linked to them;
- or a dataset's folder, or a dataset, pipeline or backtest name, starts with the project's id.

Routes:
- `GET /api/projects` lists projects with counts per kind. `POST /api/projects {name, description?, brands?, kind?}`
  makes one. With `kind` (a recipe, below) it starts with that recipe's input slots. `PUT /api/projects/{id} {name?,
  description?, archived?, brands?}` changes one. `DELETE /api/projects/{id}?force=false` deletes one (below).
- `GET /api/projects/{id}` lists everything in it, by kind, including registry models.
- `GET /api/projects/{id}/lineage` returns the project's models, their parents and architectures, the pipelines
  serving them, and the backtests that compared those pipelines.
- `GET /api/projects/{id}/state` returns the state doc (`projects/<id>/state.yaml`, compare-and-swap):
  `{project, kind, recipe:{id, label, source}, slots:[slot], inputs:{slot: input}, pins:{slot: ref|null},
  evaluation:{method}, experiments:[...], champion, champion_history, registry:{arch?, brand?, uri?},
  trainer_suggestion}`. Pins are exact refs (`name@vN`, a holdout `name@vN:holdout`). Projects made before kinds have
  `kind: null` and the five slots catalog, events, queries, labels and holdout.
- `POST /api/projects/{id}/pin {slots?:{slot: ref}, catalog?, events?, queries?, labels?, holdout?, arch?, brand?,
  registry?, dry_run?}`
  pins inputs (a name alone pins its newest version; `""` or null unpins) and sets where published versions go.
  Returns `{project, pins, changed:{slot:{from, to}}, registry?}`. A dataset of the wrong kind, or a holdout that
  isn't sealed, is a 400.
- `GET /api/projects/{id}/stages`: where the project stands, from the state doc and the lineage index only (it
  never lists the registry). A newer version of a pinned input is `info`, not an error:
  ```
  {project, kind, recipe, index_complete, notes:[{code, text}],
   next:{action: set-input|add-evaluation|new-experiment|make-champion|backtest, slot?, experiment?, text},
   evaluation:{method, chosen, ready, label, evidence, text, missing:[slot ids]},
   inputs:[{slot, label, role, group: data|labels|holdout, accepts:[kinds], required, why, origin, pinned,
            mode, from, rows, fingerprint, as_of, last_read:{at, rows, fingerprint}|null,
            status: pinned|live|missing|empty, newest,
            info:[{code: newer-version|made-from-other-version, ref, text}],
            next: null | {action: "pin", slot, ref?}}],
   experiments:{count, by_status:{trained?, backtesting?, evaluated?, failed?},
                next:{action: new-experiment|make-champion|backtest, experiment?}},
   champion:{pinned, experiment, info:[{code, ref, text}]},
   ship:{registry:{arch, brand, uri}, ready, online: "not yet confirmed online"}}
  ```
- `GET /api/projects/{id}/experiments`: `{project, champion, pins, experiments:[row]}`. A row is the experiment
  `{id, pipeline, training_set, labels, trainer, backtest, run, job, status, note, created_at, created_by,
  is_champion}` plus `evaluation` (null until a run lands):
  ```
  {run, against, against_is_champion, metric, value, champion_value, delta, ci_low, ci_high, p,
   comparison: win|loss|noise, verdict, recall:{metric, value, champion_value}, judged_at_10, evidence,
   label_independence:{status: independent|shared|unknown, text, train, train_source, holdout, holdout_source},
   online: "not yet confirmed online", click_grades}
  ```
  `verdict` is "beats the champion", "loses to the champion" or "no clear difference from the champion". When the
  training labels and the holdout grades share a labeler (`label_independence.status` shared), it is "agrees with
  its own labeler" whatever the comparison says, and `GET /api/runs/{run}/compatibility` adds an `own-labeler` warning.
- `POST /api/pipelines/{name}/retrain` takes `project`: the version it trains is recorded as that project's
  experiment when it lands on this computer (`result.experiment`, or `result.experiment_error`).
- `POST /api/projects/{id}/experiments {pipeline, training_set?, labels?, trainer?, note?, dry_run?}` records a
  trained version as an experiment. `labels` (or the version's training inputs, or the training set's provenance)
  is what label independence reads.
- `GET /api/projects/{id}/champion` returns `{project, champion, history}`. `POST /api/projects/{id}/champion
  {experiment | pipeline, expect?, dry_run?}` makes one the champion. With `expect` (the champion the page showed),
  a champion that changed since is a 409 and nothing is written.
- `POST /api/projects/{id}/experiments/{eid}/backtest {compute?, dry_run?}` judges the experiment against the
  champion by the project's evaluation method (below), as a job: `{job, live, experiment, backtest}`. The run (or the
  compute job) and what it read (`eval_inputs`) are recorded on the experiment. With `dry_run`, returns the plan with
  its label independence.

### What a project builds, and where its inputs come from

- **Recipes.** `GET /api/recipes` returns `{recipes:[{id, label, description, source: builtin|workspace|repo,
  slots:[slot], trainers:[{id, label, command, why}], evaluation:{method, holdout, text}}], errors:[{file, source,
  error}]}`. Built in: `search-events`, `search-judgments`, `recommendations`, `byo-pipeline`. A team adds its own as
  YAML in the workspace (`recipes/<id>.yaml`) or its repo (`captain/recipes/<id>.yaml`). `cst projects kinds` lists
  them. A slot is `{id, label, role, accepts:[dataset kinds or any], required, why, origin: recipe|custom}`. `role`
  (catalog, events, queries, labels, holdout, users, items, interactions, custom) is what evaluation reads.
- **Slots belong to the project.** `POST /api/projects/{id}/kind {kind}` copies a recipe's slots and evaluation (inputs
  whose slot id remains are kept). `POST /api/projects/{id}/slots {id?, label, role?, accepts?, required?, why?}`
  adds one, `PUT /api/projects/{id}/slots/{slot}` changes one, `DELETE /api/projects/{id}/slots/{slot}` removes an
  optional one (a required one is a 400: make it optional first). Each returns the state.
- **Sources.** `GET /api/sources` returns `{connections:[{name, engine, label, offers, tables_cached, tables_route,
  events_route, suggestions_route}], datasets:[{name, version, kind, rows, created_at, by_reference}],
  folders:[{path, label, files}]}`, from cached facts only. `offers` is `[table, sql]`, `[event, sql]` (PostHog) or
  `[report]` (Google Analytics). `GET /api/sources/{conn}/events?days=30` returns `[{event, count}]`.
- **Inputs.** `POST /api/projects/{id}/inputs/{slot} {source, mode: snapshot|live, as_of?, kind?}`. `mode` is
  required: there is no default. `source` is `{from: connection, connection, table | sql | event:{name, days?} |
  report:{...}}`, `{from: local, path, table?}` (a file or folder of parquet, csv, jsonl or json, or a .duckdb file
  with `table`) or `{from: dataset, ref}`.
  - A snapshot copies the rows into a new dataset `<project>/inputs/<slot>` and pins it. From a connection or a path
    it runs as a job: `{slot, mode, job, input: null}`, whose result is `{slot, input}`.
  - Live stores a reference. A connection keeps its query, with the row count and a fingerprint of the query and
    count. A path is registered by reference (sizes and modified times). A dataset follows its newest version.
    These answer at once: `{slot, mode, job: null, input}`.
  - `input` is `{mode, from, source, ref, rows, fingerprint, as_of, pinned_at, pinned_by}`. `ref` is null for a live
    query. `DELETE /api/projects/{id}/inputs/{slot}` clears one. An input pinned to a generated dataset (tagged
    `synthetic`) also has `simulated: true`, and so does its row in `/stages`.
- **Synthetic events.** `POST /api/projects/{id}/inputs/{slot}/generate {sessions?: 20000, days?: 30, seed?: 7,
  end?: YYYY-MM-DD}` generates SIMULATED shopper events from the project's catalog and sets them as that events
  input, as a snapshot. It runs as a job: `{slot, job, simulated: true, catalog, dataset}`, whose result is
  `{slot, input, dataset, stats, simulated}`. 400 when the project has no catalog, the slot doesn't take events, or
  `sessions` (1 to 200,000) or `days` (1 to 365) is out of range.
  - The dataset is `<project>/synthetic-events`, kind `events`, one row per impression: `session_id, user_id, ts,
    device, query, query_class, product_id, position, clicked, added_to_cart, purchased, price, simulated`. It is
    tagged `synthetic: true` and `label_source: simulated`, and its provenance holds the generator and its version,
    the seed, the exact catalog version (its lineage edge) and every parameter. The same catalog version, seed,
    sessions, days and end date give the same rows.
  - A project judged on it reads `evaluation: {method: logged-clicks, label: "Replay of logged searches, graded
    from clicks", evidence: SIMULATED, simulated: true}`, and every run's evidence is `SIMULATED`. A trainer gets
    `label_source: simulated` for the input.
- **What runs read.** A retrain started with `project` and no `inputs` trains on the project's inputs, live ones read
  again at that moment (a folder that changed is registered as a new by-reference version). Each trainer input, and
  the experiment's `inputs_read`, is `{slot, role, ref, mode, rows, fingerprint, as_of, changed_since_pin, read_at}`,
  and the input's `last_read` is updated. A live
  query reaches the trainer as `{ref: null, source:{connection, query}, rows, fingerprint}`, and the trainer reads it
  itself. A backtest records `eval_inputs` the same way. A query's fingerprint covers its text and row count only.
- **Evaluation.** `PUT /api/projects/{id}/evaluation {method}` with `auto`, `holdout`, `logged-clicks`,
  `trainer-reported` or `none`. `auto` takes the first that can run:
  1. judgments with a sealed holdout (`holdout`), or with unsealed queries (`judged-queries`, labelled as such);
  2. a replay of an events input graded from clicks (`logged-clicks`, LOGGED_CLICKS). When the experiment's training
     inputs carry `as_of` and the logs have a timestamp, only later searches are replayed;
  3. metrics the trainers reported (`trainer-reported`). It has no interval, so the verdict only says which number
     is higher.
  With none of them, `stages.evaluation` says "No evaluation set yet" and a backtest is refused with that sentence.
- **Delete.** `DELETE /api/projects/{id}?force=false` removes the project's entry, its membership and brand claims, and
  its state. Datasets, pipelines, models, runs and connections stay, unclaimed. Returns `{deleted, kept:{datasets,
  pipelines, backtests, models, notebooks, connections}, forced_past}`. While a live handoff of its champion or an
  experiment has no deployment linked, it is a 409 `{error, handoffs:[{id, pipeline, environment}]}` unless `force`.
  `cst projects delete <id>` asks first (`--yes` skips the question).
- **Assisted setup.** `POST /api/projects/{id}/assist {ask?}` returns `{assist, project, prompt, folder}`. The app
  opens a coding agent with `prompt`. The agent files `cst projects propose <id> --file proposal.yaml --assist <assist>`,
  which changes nothing. `GET /api/projects/{id}/proposals` lists `[{id, assist, agent, by, at, status:
  proposed|accepted|rejected, note, slots:[{slot, add, label?, role?, accepts?, required?, source?, mode?, why}],
  remove_slots, trainer:{command, script:{path, key, bytes}, why}, evaluation:{method}}]`.
  `POST /api/projects/{id}/proposals/{prop}/accept {only?}` applies it and returns `{proposal, state, applied:{added,
  changed, removed, pinned:[{slot, ref}], skipped}, to_connect:[{slot, source, suggested_mode, why}], trainer}`.
  `GET /api/projects/{id}/proposals/{prop}/script` returns the proposed training script `{path, key, text}`.
  Dataset sources are pinned on accept. Connection and local sources come back in `to_connect`, for the person to
  choose snapshot or live. `POST .../reject {note?}`. An agent session can propose, but can't accept or reject.
- `?project=<id>`, or `?project=none` for unclaimed items, filters `/api/datasets`, `/api/datasets/tree`,
  `/api/pipelines`, `/api/backtests`, `/api/investigations`, `/api/models` and `/api/connections`.
- `POST /api/datasets/import` and `POST /api/sql` with `save` accept `project`, which puts the result in that project.

## Scope model, engines and the Explorer

- `GET /api/explorer` returns the navigation tree: All engines, All experiments, scope groups (Brand > Site > Engine
  by default), unscoped engines and Projects. It also returns `actions` (Add brand, Add site,
  Add engine) and the model's `model_revision`.
- `GET /api/scope-model` returns the model. `PUT /api/scope-model {types, explorer, engine_types, expect_revision?}`
  replaces it, and is refused with reasons on unknown parents, loops, removed types still in use, or a stale revision.
  Each type is `{key, singular, plural, description, parents, fields}`. Keys are stable, and labels are free to change.
- `POST /api/scopes {type, name, parent?, fields?}` adds a scope. `PUT /api/scopes/{id} {name?, parent?, archived?,
  fields?}` renames, moves (validated) or archives one. A scope's `fields.registry_brands` maps registry brands to it.
- `GET|POST /api/engines`, `PUT /api/engines/{id}`: engines are persistent identities `{name, type: search |
  recommendation, scopes, pipelines}`. A pipeline belongs to the engine that lists it.
- `POST /api/scopes/link {items, scope? | engine?}` attaches records to a scope or an engine.
- **Filtering by the Explorer's pick:** send `X-CST-Scope: scope:<id> | engine:<id> | project:<id> | view:<id>` (or
  `?scope=`) on any list route: datasets, the tree, pipelines, backtests, investigations, models, connections,
  runs, grading queues and signals. A
  scope includes its descendants. `view:` filters nothing. Anything else gets a 400.

## Lineage and quality (Unity Catalog style)

- `GET /api/lineage/graph?node=<id>&up=2&down=2` returns the nodes within `up` steps upstream and `down` downstream,
  each with `level` (negative is upstream), plus `more_upstream` and `more_downstream`. Without `node`, it returns the
  whole graph.
  - Node ids: `table:<conn>/<schema.table>`, `dataset:<name>@v<n>` (or `dataset:<name>` for the newest),
    `pipeline:…`, `run:…`, `model:<brand>@<v>`, `connection:…`, `environment:…`, `architecture:…`.
  - Edges come from Captain's records, attached registries, the SQL that made each dataset (warehouse table, then
    dataset), and imported warehouse lineage.
- `GET /api/lineage/columns?dataset=&column=` returns `{sources: [{table, column, node}], path, known, reason}`. It
  traces the dataset's SQL with SQLGlot against the pulled table metadata.
- `POST /api/lineage/import/{conn} {days?}` returns `{job}`. It reads Databricks Unity Catalog's
  `system.access.table_lineage` and `column_lineage`, or Snowflake's `ACCOUNT_USAGE.ACCESS_HISTORY`, with the
  connection's own login, read-only. Other databases get a plain refusal.
- `GET /api/profile?dataset=<name>[@vN]` returns rows, age, and per column the type, null share, distinct count, min
  and max.

## Import from sources: suggested sample queries and previews

- Connection kinds now include **PostHog** (HogQL over its query API, with a personal API key stored as a secret,
  guarded read-only) and **Google Analytics 4** (Data API reports: the "query" is JSON
  `{dimensions, metrics, start, end, filter: {dimension, equals}, limit}`). Both work with previews, saving, the
  query log and the table browser. GA4's BigQuery export is recognized when BigQuery is connected.
- `GET /api/connections/{name}/suggestions` returns `{pulled, suggestions: [{id, title, why, kind, query}]}`.
  Suggestions come from the read tables: search logs (top searches, zero-result searches, per day), catalogs
  (sample, per category), event streams (per day, recent), the GA4 BigQuery export (site search terms), or a small
  sample. PostHog and GA4 get their own. Every query is bounded by a LIMIT or a time window. `pulled: false` means
  read the tables first.
- `POST /api/connections/{name}/preview {query, limit<=1000}` returns `{columns, rows, row_count, limited, ms}` and
  saves nothing. Save with `POST /api/sql {conn, sql, save, kind, project?}`.

`GET /api/runs?backtest=&project=&limit=` lists every backtest run, newest first: `{name, version, created, mode,
compute, arms, complete, wall_s, by}`. Grading queues are in scope when linked there or when they grade an in-scope
pipeline. Signals are in scope when linked there or when they're evidence on an in-scope investigation.

## One shape for rows, before and after import

Every route that returns rows answers in the same shape (backend/core/captain_search/data/cells.py): dataset rows,
warehouse table rows, query previews, samples and `POST /api/sql`. The shape is `{columns: [{name, type, kind}], rows,
offset, limit, has_more, total?, ms, filtered, sql?}`.
- `kind` is one of text, integer, number, decimal, bool, date, time, timestamp, list, struct, binary or other. It's
  the same whether the source names the type in Arrow or DuckDB terms.
- Cells: lists and structs are JSON text, decimals are exact text, times are ISO 8601, NaN is null.
- `GET /api/tables/{conn}/{schema}/{table}/rows?offset=&limit<=1000&order=&desc=&where=&count=` pages through a whole
  warehouse table, every column. The page query is translated per database. `/sample` is its first page.
- `POST /api/connections/{name}/preview` returns the same page, plus `row_count` and `limited`.

## Bring your own rankings (spec S2)

A team's own system's results, imported and compared like any backtest. The contract is one long table (csv, tsv,
parquet, json or jsonl), one row per (query, arm, item):
- **Required:** `query_id`, `arm` (the system or version), `item_id`, and `rank` (1 is the top) or `score` (higher is
  better).
- **Optional:** `query` (text), `label` (grade 0 to 3), `segment` (a slice) and `weight` (traffic). Map your own column
  names with `mapping`.

Routes:
- `POST /api/rankings/validate {path | dataset, mapping?, judgments?}` returns `{ok, errors, warnings, stats,
  available}`, in plain words. It catches missing columns, empty ids, duplicate items, tied or bad ranks, labels
  outside 0 to 3, conflicting labels, and arms missing queries. `available` says which analysis the data supports. With
  no labels or judgments, relevance metrics are unavailable, never invented.
- `POST /api/rankings/import {name, path | dataset, mapping?, baseline?, labels_from?, judgments?, catalog?,
  primary_metric?, mde?, hypothesis?, project?}` returns `{job}`. Its result is `{run, backtest, datasets, validation,
  evidence}`.
  - It versions the raw file and builds query and label datasets.
  - It publishes a run (mode `rankings`) with every arm's rankings stored. Comparisons, slices by `segment`, the query
    list, side by side, the histogram and compatibility all work on it.
  - `labels_from` (human, llm, clicks, heuristic) is required with labels, and sets the evidence label.
  - `catalog` gives titles side by side.

CLI: `cst rankings validate <file> [--map query_id=qid,arm=system]` and
`cst rankings import <name> <file> --baseline prod --labels-from human`.

## Prepare data for training

Pull a warehouse table, a query or a dataset in as clean, training-ready data.
- `POST /api/prepare/preview {source: {conn, table: "schema.name"} | {conn, sql} | {dataset}, role?, mapping?,
  options?}` returns:
  - the role (guessed if not given: catalog, queries, judgments, events or generic) and every role's required and
    optional columns;
  - the column `mapping`, guessed from common names and editable, and what's `missing`;
  - a `report` of every change (rows in and out, the steps in words, problems and warnings);
  - the first 100 cleaned rows as a page.

  It runs on a 1,000-row sample, and nothing is saved.
- `POST /api/prepare/import {source, role?, mapping?, options?, name?, folder?, project?}` returns `{job}`. It
  prepares the whole source the same way and saves a dataset of the role's kind in the role's folder (catalogs,
  queries, judgments, logs, imports). Provenance records the source, role, mapping and cleaning steps. It's refused
  if required columns are unmapped or cleaning leaves no rows.
- Cleaning (`options`, on by default): text is trimmed and empty text becomes null, types are cast, rows missing a
  required column are dropped, grades outside 0 to 3 are dropped, and duplicates on the role's key keep their first
  row. Source order is kept. `keep_other_columns` defaults to true.

## Query debugger: why did it rank this? (plan 2)

- `GET /api/explain?pipeline=&q=&vs=&query_id=&judgments=&k=` returns a trace per stage:
  `{pipeline, engine, query, stages: [{stage, kind, explained, notes, items: [{id, rank, score, score_parts, fired,
  moved, title, grade?}], timing_ms, ...}]}`.
  - Stage kinds: understand (tokens, terms, expansions, synonyms, unknown terms), retrieve, rules, filter (with
    `removed`) and final.
  - Local BM25 is exact: each item's `score_parts` are per field and term, and they add up to its score.
  - Elasticsearch uses its own `explain`, the top 8 parts per hit.
  - Other engines return `explained: false` with "no explanation provided", unless they implement `explain(query, k)`.
  - With `vs`, it returns `{a, b, first_divergence, graded, note}`. `first_divergence` is the first stage where a
    relevant result (grade 2 or more) was lost or ranked lower.
- `GET /api/runs/{run}/queries/{qid}/explain?a=&b=` explains one query of a comparison run, with its grades. Arms from
  imported rankings can't be replayed, and the error says so.

CLI: `cst explain <pipeline> -q "..." [--vs <pipeline>] [--query-id --judgments]`.

## Profiler: where the time goes (plan 5)

Every stage in the query debugger's trace now carries `timing_ms`. Retrieval adds `candidates`, and the final stage
adds `total_ms`.
- `POST /api/runs/{run}/profile {max_queries?, memory?}` returns `{job}`. Its result is `{run, queries, arms:
  [profile], skipped, summary, measured_on}`. Arms from imported rankings are skipped with a reason. `summary` reads
  like "X adds 4.0 ms at p99 against Y, most of it in boosts (+3.8 ms)".
- `POST /api/profile/pipeline {pipeline, queries: dataset ref | [texts], memory?}` returns `{job}`. Its result is one
  profile.
- A profile is `{pipeline, engine, queries, explained, total: {p50_ms, p95_ms, p99_ms, mean_ms}, stages: [{stage,
  kind, p50_ms, p95_ms, p99_ms, mean_ms, share}], slowest: [{query, total_ms, dominant_stage, stages_ms,
  candidates}], memory: {peak_python_bytes_p50, peak_python_bytes_max, what}, budget, measured_on}`.
- Budgets live in the pipeline spec as `budget: {p99_ms: 120, stages: {retrieve: 30, rules: 5}}`. Stage keys are kinds
  or names. `budget` returns `{ok, breaches: [sentences]}`.
- Every number is offline replay on this computer, at concurrency 1, and says so. Memory is Python allocations
  (tracemalloc), not process RSS.

CLI: `cst profile <run>` or `cst profile <pipeline> --queries <dataset:split>`.

## Living evaluation sets (plan 4)

- `POST /api/evals/health {queries, judgments, logs?, catalog?, lifetime_days?}` returns `{status, findings, ...}`:
  - `representativeness`: the share of searches covered overall and by head, torso and tail, the top unseen
    queries, and set queries nobody searched;
  - `staleness`: grades on retired products, products changed since grading (title, price, stock), relevant products
    now out of stock, and grade age;
  - `coverage`: judged@10 per arm of runs scored with these grades;
  - `reliability`: agreement on pairs graded twice, or a plain note that it can't be measured.
- `POST /api/evals/refresh {queries, judgments, logs, catalog?, add?, seed?, dry_run?}` returns `{job}`. It publishes
  new versions and never edits. The queries get new traffic samples (stratified by band, seeded) in a new `added`
  split, and the sealed holdout is unchanged. The grades drop retired products. The result lists what to regrade, plus
  a changelog.
- Rubrics: `captain/rubrics/<domain>.md`, listed at `GET /api/rubrics`.
  - `cst judge run --rubric <domain>` prompts the LLM judge with it and records the rubric's name and hash in the
    prompt version.
  - Grades from different rubrics, or with and without one, are refused in one dataset.
  - `POST /api/grading {..., rubric}` stores the rubric on the queue, so graders see the same words.

CLI: `cst evals health <queries> <judgments> --logs --catalog`, `cst evals refresh ... [--plan]`.

## What-if sandbox (plan 1)

- `POST /api/sandbox/sweep {pipeline, queries, judgments, knobs?, sample?, top?, seed?, metric?}` returns `{job}`.
  - Knobs are spec paths (`k1`, `fields.title`, `boosts.0.weight`) with `{values}` or `{min, max, steps}`, at most
    three and 64 combinations. Without knobs, it uses the pipeline's `spec.tunables`.
  - Pass 1 runs every combination on a stratified sample. Pass 2 runs the best `top` on every query.
  - Each cell has `mean`, `delta` against the current values, `ci`, `verdict` and `n`. Full-pass cells add
    `slice_flips`. The result carries the grades' `evidence` and a note that it's offline.
- `POST /api/sandbox/ripple {pipeline, queries, judgments, values, must_not_break?}` returns `{moved_most,
  regressions, must_not_break, broken, summary}`, query by query.
- `POST /api/sandbox/promote {pipeline, values, note?}` publishes the setting as the pipeline's next version, so a
  backtest can test it. Nothing goes live.

CLI: `cst sweep <pipeline> --queries Q --judgments J --knob k1=0.8:2.0:4 --knob boosts.0.weight=1,1.2,1.5`.

## MLflow bridge (read-only)

- `PUT /api/mlflow {uri, token_secret?}` or `{databricks_connection}` sets where Captain reads MLflow from. A
  Databricks connection's host and token are reused, and the token needs the `mlflow` scope.
- `GET /api/mlflow` returns the settings and links, without calling MLflow.
- `GET /api/mlflow/experiments`, `GET /api/mlflow/experiments/{id}/runs?limit=`: runs with status, times, metrics,
  params and non-system tags.
- `GET /api/mlflow/models`: registered models, with each version's run, status, stage and aliases.
- `POST /api/mlflow/links {model, version, pipeline}` records which pipeline serves a model version.
  `GET /api/mlflow/lineage` returns nodes and edges, MLflow run to model version to pipeline.
- Captain only calls MLflow's search endpoints. It never writes to MLflow.

CLI: `cst mlflow use --uri URL | --databricks CONN`, `cst mlflow experiments|runs <id>|models`,
`cst mlflow link <model> <version> <pipeline>`.

## Settings and connected services

- `GET /api/services?check=` returns, in one list:
  - storage;
  - connections (kind, writable, network);
  - compute targets, with `default_for` and each kind's `live_tested`;
  - compute `defaults`, the available `kinds`, registries, MLflow (no links) and model keys (set or not).
  `check=true` runs select 1 on each connection and each target's test. Secrets are never included.
- `PUT /api/settings/serving {host, machine}`: the machine a deploy on that host (`aws`, `modal`, `gcp`, `azure`)
  uses, in place of the smallest the model fits on. `machine` is a name from `machines` in
  `GET /api/projects/{id}/deploy/hosts`. Empty goes back to the smallest that fits. `{name_prefix}` sets the start
  of the name of everything a first deploy makes in a cloud account ("captain" when empty).
- `GET /api/settings`, `PUT /api/settings/compute {backtest?, serve?}`: the compute that backtests and serving use
  unless they name their own. They're saved in the bucket (`settings.yaml`), so every teammate and session gets them.
  A target must list the purpose.
- New compute kinds:
  - `databricks-jobs` triggers an existing job that runs cst, through a Databricks connection;
  - `snowflake-spcs` runs the runner image as a job service in a compute pool, through a Snowflake connection.
  Neither has been tested on a real account yet.
- The scope model starts empty. `GET /api/scope-model/suggested` returns Brand, then Site, to offer as a one-click start.
- `GET /api/projects/{id}/home` returns `{project, datasets, pipelines, models, mlflow_models, notebooks, runs,
  connections, investigations, counts, empty}` for a project's home screen. Saved notebooks can now be assigned to
  projects (kind `notebook`).

## Finding and making a model registry (AWS)

- `GET /api/registries/discover?profile=` lists every S3 bucket the login can see, registries first. Each bucket is
  `{bucket, region, kind, uri, detail, attached}`, where kind is one of:
  - `registry`: models in `<architecture>/<brand>/<version>/MANIFEST.yaml`, at the root or under a folder (`uri` points
    at that folder), or an `INDEX.md` or `.captain/registry.yaml`;
  - `empty`;
  - `workspace`: a Captain workspace, whose datasets share that folder shape;
  - `other`;
  - `no-access`.

  It only reads. On a 91-bucket account it takes about 15 seconds. CLI: `cst registry discover`.
- `GET /api/registries/template?bucket=&region=&format=terraform|cloudformation` returns `{format, filename, text,
  how}`: the registry bucket as infrastructure code, for a team's own pipeline to apply.
- `POST /api/registries/create {bucket, region, profile?}` is a job. It makes the bucket through a CloudFormation stack,
  `captain-model-registry-<bucket>`. The bucket is:
  - versioned and encrypted (SSE-S3);
  - fully private, with the bucket owner owning every object;
  - closed to plain HTTP;
  - kept when the stack is deleted.

  The job then writes `INDEX.md` and `.captain/registry.yaml` and attaches the bucket, which has 0 models.
  - CLI: `cst registry create <bucket> --region us-west-1`.
  - `--terraform main.tf` or `--cloudformation t.yaml` writes the code instead.
  - The login needs `cloudformation:CreateStack` and the S3 bucket-configuration permissions.

A registry with an `INDEX.md` and no models yet attaches without error. Before this change it was refused.

## Live runs (Runs, Live tab)

A run streams events while it goes, and Runs draws them: stages, progress and time left, each metric per arm with
its line, simulated shoppers side by side, and the log. Captain's backtests stream (from the app, from
`cst backtest run`, and when a recommendation runs). Any tool can too:

- `cst live pipe --title "ranker v7" --kind training -- python train.py`: a printed JSON object with a `"type"` is
  an event, every other line is a log line;
- `cst live start --title ... --kind ...` prints an id, then `cst live emit <id> metric -d '{"name": "loss", "value": 0.3, "step": 100}'`
  and `cst live end <id>`;
- from Python, `captain_search.live.start(kind, title)` returns a run with `stage`, `progress`, `metric`, `log`,
  `emit` and `end`.

Events: `stage {name, index, of}`, `progress {fraction, done, total, unit}`, `metric {name, value, arm, step}`,
`shopper {arm, shopper, query, results, steps, clicked, bought, revenue}`, `log {text}`, `result {ref, summary}`.
Routes: `GET /api/live?active=&limit=` and `GET /api/live/{id}?since=n` (events from position n, and `next`).

Training streams the same way. `cst pipeline retrain` (or Retrain on an engine's pipeline, `POST
/api/pipelines/{name}/retrain`) runs the pipeline's `spec.train.command` and streams its output: a printed
`{"type": "metric", "name": "loss", "value": 0.31, "step": 200}` is a point on the run's chart, any other line a log
line, and the `metrics` it writes to `CST_TRAIN_OUT` show as the run's final numbers.

On a compute target (AWS, Google Cloud, Azure), the app makes the run in the workspace bucket before it submits the
job and passes its id as `CST_LIVE_ID`; the job's container writes events there in batches every 2 s
(`live/<id>/<seq>.jsonl`, header `live-heads/<id>.json`), and Runs reads only the new batches. Inside a job
(`CST_JOB_ID` set) a backtest or retrain always runs in place, never submitting itself again.

## Images in data

`GET /api/datasets/{name}/media` finds a dataset's image columns: full addresses (https://, s3://, gs://, a file
path) and bare file names ("main.jpg"). For bare names it says where they live: the address saved for the column,
else one Captain found next to the dataset's file (`.../images/{pid}/{value}` and similar), checked on the first row.
`PUT` the same route with `{column, template}` saves one, after checking the first row's image exists. In a template,
`{value}` is the cell (each item of a list) and `{<column>}` any column of the row.
`POST /api/media/thumbs {srcs, size, dest}` makes JPEG thumbnails (64, 160 or 480 px) into the app's `cache/thumbs`
folder, through the workspace's login for buckets, 8 at a time and cached by address. The grid shows the first image
of each cell and an opened row shows all of them.
