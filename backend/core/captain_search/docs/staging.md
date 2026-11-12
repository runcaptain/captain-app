# Staging: a model version as a live endpoint, in your own cloud

Staging runs one model version as a live endpoint you can query, beside the project's baseline. It runs on your
compute target (a SageMaker endpoint in your AWS account) or, with `--target local`, as a process on this computer.
Captain hosts nothing and never deploys to production: launch review is still the gate before that.

    cst projects stage <project> [--candidate <id>|--baseline] [--env staging] [--target <compute target>|local]
    cst projects staging <project>                  # what runs, where, since when, and when it stops by itself
    cst projects staging-query <project> "<query>"  # the staged results beside the baseline's
    cst projects staging-check <project>            # did serving compute the features training computes?
    cst projects staging-connect <project>          # how a site calls it, and how many real requests have arrived
    cst projects shadow-start <project>             # shoppers keep the baseline; the candidate ranks silently
    cst projects shadow <project>                   # what the candidate would have changed
    cst projects ab-start <project> --split 5%      # an A/B test between the baseline and the candidate
    cst projects ab-readout <project>               # the readout
    cst projects ab-ramp <project> 25               # change the candidate's share; ab-rollback sends everyone back
    cst projects ab-decide <project> ship           # or keep-baseline, or keep-testing
    cst projects unstage <project>                  # stop it and remove everything Captain created

The app does the same from the project page (Ship: 1 Stage, 2 Try it, 3 Connect your site, 4 A/B test, 5 Launch
review), through
`POST /api/projects/{id}/stage`, `/unstage`, `/staging/query`, `/staging/check`, `GET /api/projects/{id}/staging`,
`GET /api/projects/{id}/ab` and `POST /api/projects/{id}/ab/start|ramp|rollback|stop|simulate|outcomes|result`.

## What can be staged

| The version | What happens |
|---|---|
| A keyword-search version, or a starter ranker (`local-bm25`, `bm25-ltr`) | Captain's runtime serves it: keyword search finds the candidates, the trained model reorders the top K with features from the module that built its training set |
| A version that ranks by calling a service you run (`http`, `elastic`, ...) | Wrapped: Captain's runtime sits in front of your service and does the logging and the split |
| A model trained elsewhere with no serving entry point | Refused in a sentence. Bring `--endpoint <url>` (any service that answers `POST {query, k}` with `{ids}`) or `--image <uri>` (a container that follows SageMaker's serving contract) |

`--endpoint` runs Captain's runtime on this computer in front of your service. `--image` deploys your container to
the SageMaker target as it is: Captain can query it, but it logs nothing and cannot split traffic inside it.

## The serving bundle

`stage` writes a bundle to the workspace's storage, `staging/<project>/bundles/<id>/`: `bundle.json` names each arm
(baseline, candidate) with its model version and files, and the artifact files (the trained model, its feature spec
and lookup tables) are copied beside it. The catalog is stored once per version under `staging/<project>/catalogs/`.
A local endpoint and a cloud endpoint load the same bundle, so what you try here is what the cloud serves.

## The endpoint

| Route | What it does |
|---|---|
| `POST /rank` | `{query, k?, user_id?, session_id?, request_id?, traffic?}` gives `{request_id, ids, scores, arm, model_version, ms}` |
| `POST /rank/<arm>` | The same from one arm, `baseline` or `candidate` |
| `POST /outcome` | `{request_id, event: click or add-to-cart or purchase, product_id?, value?}` |
| `POST /flush` | Write buffered log records now. `{"publish": true}` also publishes the logs dataset |
| `GET /healthz` | What is loaded, uptime, requests served, seconds until it stops by itself. No secrets |

Every route but `/ping` and `/healthz` needs the endpoint key: `Authorization: Bearer <key>` on a local endpoint.
SageMaker exposes one route, so there the body carries it: `{"op": "rank", "key": "<key>", "query": ...}` to
`InvokeEndpoint` (ops: `rank`, `outcome`, `flush`, `status`, `reload`). The key is generated at stage time and kept
in this computer's keychain as `STAGING_<PROJECT>_<ENV>_KEY`; the endpoint only ever holds its SHA-256. On a machine
with no keychain, set that name in the environment before staging and that value is used. A local endpoint binds to
127.0.0.1 only.

`traffic` says where a request came from: `real` (the default), `simulated` or `test`. The Try it box sends `test`.
Only `real` ever counts as REAL_TRAFFIC evidence.

## Where it runs: this computer, or your cloud

Before staging, Captain offers the places a model can run with what each costs: this computer (free, only you can
reach it) and the connected cloud (an hourly estimate, a site can reach it). The last choice is remembered per
project.

**A big model is staged in the cloud, not on this computer.** There is a size limit for staging here, separate from
free memory: by default a model that needs more than 1 GB to serve (the estimate below, with headroom, counting the
baseline beside it) is not offered on this computer, even when the memory is free. The choice is switched off with
the reason and a link to the setting (Settings, Costs: "Largest model to stage on this computer": 250 MB, 500 MB,
1 GB, 2 GB, 4 GB or No limit). `cst projects stage --target local` refuses the same way unless `--allow-large` is
passed. With a cloud connected the cloud is offered first.

**This computer is only offered when the model fits.** A local endpoint holds the catalog, a keyword index for
each version and a trained ranker's feature tables in one process. Captain estimates that from the catalog's size
and compares it with the memory and disk free right now. Measured 2026-11-11 on demo-shaped catalogs: about 8.0 KB
a product for a trained ranker, 4.5 KB a product for a keyword version, 0.45 KB a product for the rows, 120 MB for
the process, plus 30% headroom. A 200,000 product catalog with both versions needs about 3.4 GB, loads in about 7
seconds, and answers a query in about 120 ms. If it won't fit, the choice is shown switched off with the reason
("This catalog needs about 9 GB of memory to serve, and this computer has 3 GB free. Stage it in your cloud."). If
it is tight, it is allowed with a warning. Staging is refused before anything starts when it won't fit, and an
endpoint that runs out of memory while loading says so.

**Set up serving in your AWS account** (`cst compute setup aws-serving --profile <login> --region <region>`, or
the button on the deployment page) creates what a SageMaker staging target needs, after showing exactly what and
a confirm: the role the endpoint runs as (it can read the serving bundle, write the request log, pull its image and
delete its own endpoint, and nothing else), a repository for the serving image, the image, and the compute target.
The image is built inside your account by AWS CodeBuild from the installed Captain (measured: 78 seconds, about
$0.01),
so no Docker is needed on your computer. Everything is named `<prefix>-serving...` and tagged `app=captain`.
`--remove` takes it all away again and is refused while a model is staged there. `--policy` prints the exact
policy your login needs, for an admin. A login that lacks a permission is told which step and which action.

## Cost, and stopping by itself

Before any cloud deploy Captain shows the instance type, an hourly estimate and "This keeps billing until you stop
it", and starts nothing without a confirm (`--yes`). The estimate is the shared one every form uses
(`POST /api/costs/estimate`, kind `staging`): the live AWS price for the target's region when it can be read, else
the bundled table with its date. When an endpoint is stopped, what it cost while it was up is recorded on the
project and counts in the project's running total.

An endpoint stops by itself after 240 minutes with no `/rank` or `/outcome` (`--idle-minutes` changes it). A local
process exits. In the cloud two things stop it, and one of them is enough:
- the container asks SageMaker to delete its own endpoint, which needs `sagemaker:DeleteEndpoint` on the execution
  role;
- Captain stops it the next time the app or `cst projects staging` looks and finds it past its limit.

If the execution role cannot delete the endpoint and nobody opens Captain, the endpoint keeps running. Give the
role that permission.

## Checked before a cloud deploy

Before the confirm, Captain asks IAM (a policy simulation, nothing changes) whether the execution role can delete
its own endpoint and write the workspace bucket. If it cannot delete, the confirm says: "This endpoint can't stop
itself when idle. It will keep billing until you stop it from Captain." If it cannot write the bucket, it says
there will be no request log. If your sign-in is not allowed to ask IAM, it says it could not check.

## What Captain creates in AWS, and how to find it

One endpoint, one endpoint config and one model, named `<prefix>-<project>-<env>` (the target's "Name prefix for
staging resources", `captain` unless set), each tagged `app=captain`, `project=<project>`, `env=<env>`. They are
listed on the project (Details, or `cst projects staging <project>`), and `unstage` deletes exactly those and waits
until the endpoint is gone. A start that fails removes what it created before it reports. Anything that could not
be removed is named.

## Permissions the SageMaker execution role needs

| Permission | Why |
|---|---|
| Read the workspace bucket (`s3:GetObject`, `s3:ListBucket`) | The runtime loads the bundle from it |
| Write the workspace bucket (`s3:PutObject`) | The runtime writes its request log there, and publishes it as a dataset version |
| `sagemaker:DeleteEndpoint` on its own endpoint | So an idle endpoint can stop itself |
| Pull the serving image from ECR | SageMaker starts the container |

A local staging endpoint writes the same log to the local workspace store.

## The request log

Every request is logged with its request id, timestamp, query, the ids returned in order, the feature row the model
was given for each of the first 10 results, the model version, the arm and the latency. Records are buffered, written
as write-once chunks under `staging/<project>/<env>/logs/<stage>/` every 15 seconds, and published as a new version
of the dataset `<project>/staging-logs` every 5 minutes (and when the endpoint stops). The dataset has one row per
result shown, in the columns events datasets use (`query, product_id, position, clicked, purchased, session_id, ts`)
plus `request_id, arm, model_version, traffic, value, features`. A version that holds anything but real traffic is
tagged simulated, so nothing judged on it reads as real shoppers.

## Served features match training

`cst projects staging-check` recomputes, offline, the feature rows the endpoint logged, for the same (query, product)
pairs, with the feature file of the model version in the registry, and compares. It reports four measures:

| Measure | What it is |
|---|---|
| mismatch rate | share of feature values that differ between served and offline |
| missing rate | share of the values offline computes that the served row does not have |
| extra rate | share of the served values offline does not compute |
| relative error | per feature, the mean of the difference over the larger of the two values |

and one line, "Served features match training: 99.8% of 1,240 checked": the share of pairs whose whole row matched.
It passes at 99.9% or more (`--min-match`) with nothing missing or extra. A failed check is a launch criterion
(`served_features`): "Send to launch review" is refused until it passes or a person overrides it with a reason.

The pairs match on (query, product), which is right while a model's click tables are frozen at one cutoff. When
serving starts refreshing click counts, the comparison has to be made as of each request's timestamp. The request
id and timestamp are logged now so that it can be.

## Connect your site

An A/B test means nothing until a site is calling the endpoint. `cst projects staging-connect <project>` (step 3 on
the page) says where the endpoint is and who can reach it, gives the code for the two calls, and counts the real
requests that have arrived: "Waiting for the first request", then "Received 12 requests, the last one 4 s ago".
Only requests marked `real` count. The Try it box, `staging-test` and simulated shoppers are not a site.

**An endpoint on this computer answers on 127.0.0.1 only. No site can reach it.** The two ways forward are to stage
it in your cloud, or to keep testing here with simulated shoppers.

The two calls, with the `request_id` carried between them (`--code curl|javascript|python`, or `aws cli` for
SageMaker):

1. Get a ranking: `POST /rank {query, k, session_id}` returns `{request_id, ids, scores, arm, model_version, ms}`.
2. Report what the shopper did: `POST /outcome {request_id, event, product_id, value}`.

**Your site must fall back to its own search.** The snippets give the endpoint 300 ms. If it doesn't answer in
time, is unreachable, or answers with an error, show your own results. An error is a 4xx or 5xx status with the
body `{"detail": "<a sentence>"}`. A staging endpoint stops by itself when idle and is not built for uptime: a slow
or missing ranking must never hold up the page.

**The key.** The snippets read it from `CAPTAIN_STAGING_KEY` in your site's own environment, so it is never in
your code. The app's "Copy the key" button puts it on the clipboard. It is never shown on screen or written to a
log, and `cst` never prints it.

`cst projects staging-test <project>` sends one request through the same door, marked as test traffic, to prove
the path.

## Shadow mode, before any split

    cst projects shadow-start <project>
    cst projects shadow <project> [--traffic real|simulated|test]
    cst projects shadow-stop <project>

In shadow mode the endpoint answers every request with the baseline's ranking, exactly as before, and the candidate
ranks the same request silently. Both rankings are logged. No shopper sees the candidate, and a candidate that
fails never changes the answer the site gets. The readout says how many requests there were, how often the top
result and the top 10 would have differed, the average overlap of the top 10, the queries that differ most, and
both response times. It reads REAL_TRAFFIC only when the requests were real. Starting an A/B test ends shadow mode.

## An A/B test between the baseline and the candidate

Offline evaluation says a candidate is safe to try. The A/B test is the try. Staging always packs the baseline
beside the candidate, so both are already on the endpoint when a test starts.

    cst projects ab-start <project> [--split 0.01] [--primary clicks|purchases|revenue] [--guardrail <metric>[:must|watch]]... [--min-sessions 200]
    cst projects ab <project> [--traffic real|simulated|test]
    cst projects ab-ramp <project> 0.01|0.05|0.25|0.5|1
    cst projects ab-rollback <project>
    cst projects ab-stop <project>
    cst projects ab-simulate <project> [--sessions 1000]
    cst projects ab-outcomes <project> <events dataset>
    cst projects ab-result <project> --metric ... --baseline ... --candidate ... --source "..." [--traffic real|test]

**The test needs a site that is calling.** Captain's own split is refused until a real request has reached the
endpoint, because there would be nothing to split. `--practice` runs it on simulated shoppers instead: everything
it shows is SIMULATED and a practice test can't go to launch review. Shares can be written 5, 5% or 0.05, and the
form says what a share means in the endpoint's own sessions a day.

The metrics are chosen when the test starts: one primary, and the rest as guardrails. A project can measure
sessions with a click, sessions with a purchase, and revenue per session when its catalog has a price column.
Response time is always a guardrail.

Each guardrail is marked **must not get worse** or **watch**. When a "must" guardrail clearly drops, Captain pauses
the ramp and says why: the share can go down or be rolled back, and cannot go up until the guardrail holds again.
A "watch" guardrail is shown and never blocks. Response time is "must" unless told otherwise; the others are
"watch" unless marked (`--guardrail purchases:must`).

Ramp steps start at 1%: 1%, 5%, 25%, 50%, 100%. A new or expensive model goes to 1% first.

**Captain's endpoint splits the traffic** (the default). A hash of the session id (else the user id, else the
request id) and the test's id picks the arm, so a shopper always sees one version, and raising the candidate's
share only moves shoppers from the baseline to the candidate. The arm is logged on every request. The share lives
in the endpoint's control document (`staging/<project>/<env>/control.json`), which the runtime re-reads every few
seconds, so a ramp or a rollback needs no redeploy.

**Your own A/B system splits the traffic** (`--own`). Captain gives the address of each arm on the endpoint
(`/rank/baseline` and `/rank/candidate`, or `"arm"` in the SageMaker body) and records the readout you bring back
with `ab-result`. It is recorded as REAL_TRAFFIC only when you say the traffic was real.

**Outcomes** reach Captain through `POST /outcome` on the endpoint (`{request_id, event, product_id?, value?}`), or
from an events dataset whose rows carry the `request_id` that `/rank` returned (`ab-outcomes`). Rows can be one per
event (`request_id, event, product_id, value`) or one per result shown (`request_id, product_id, clicked,
purchased, price`).

**The readout** counts by session, the unit that was assigned: sessions per arm, the primary metric, the difference
with a 95% interval (the normal interval for a difference of two means), each guardrail, and whether the split is
what was asked for (a chi-square check on sessions assigned since the last ramp). It says "not enough data yet"
until both arms have the minimum number of sessions, and names no winner before that. A guardrail fails only when
the candidate is clearly worse (its interval lies below zero). The readout counts sessions from the start of the
test, across ramps.

**By segment.** The readout repeats the comparison for head, torso and tail queries, and for new and returning
shoppers. Head, torso and tail come from the project's own events log: queries are ranked by how many searches
they get and cut where the running share of searches passes one third and two thirds. A query the log never saw is
tail. New or returning needs a user id on the request and in the log. Each segment has its own difference and
interval and its own "not enough data yet", and a candidate that is ahead overall but behind on a segment is
called out in one sentence.

**Where the numbers came from** is never guessed, and traffic of different kinds is never mixed in one readout:

| The requests | Evidence | On screen |
|---|---|---|
| `traffic: real` (the default) | REAL_TRAFFIC | Real traffic |
| `traffic: simulated` (`ab-simulate`) | SIMULATED | Simulated shoppers |
| `traffic: test` (your own smoke tests) | TEST_TRAFFIC | Test traffic |

Requests from the Try it box are sent to one named arm and never enter a readout.

**Simulated shoppers** (`ab-simulate`, "Send simulated shoppers at staging") drive the endpoint with the shopper
simulator Captain already uses for simulated events: they search the catalog, look down the page, click and
sometimes buy. It shows the whole loop with no traffic yet. Everything read from it says SIMULATED, and it says
nothing about what real shoppers will do.

**How many more sessions.** While the difference is not yet clear, the readout says how many more sessions the
smaller arm needs to tell the difference seen so far from chance (80% power at the 95% level), and roughly how
many days that is at the endpoint's current rate.

**How a test ends.** `cst projects ab-decide <project> ship|keep-baseline|keep-testing` records one of three
choices. Ship it needs enough real traffic, and the readout with its evidence label is attached to the launch
review when the candidate is sent. Keep the baseline rolls the test back. After a rollback one line says what was
rolled back, why (`ab-rollback --reason`), and what to do next. The readout is filed with the test, and a new test
files the old one under the project.

**Ramp and roll back** are one call each and are written to the project's history. Rolling back during a test
sends every shopper to the baseline again and ends the test. With no test running, it restages the version that
was on the endpoint before this one.

Launch review stays the gate before production. Captain does not deploy to a production endpoint.

## When a candidate returns what the baseline returns

A first ranker often reproduces keyword search exactly. The usual cause: it was trained on simulated events, and
those events were themselves generated from keyword search, so the strongest thing to learn is "what keyword search
showed". For a query the logs never saw, the history signals are zero and the keyword score decides.

Captain says so in one sentence when a candidate's top results match the baseline's on 80% or more of a sample of
the project's own queries: on the candidate's row, in the Try it box ("Queries where these two differ most" is then
empty, and says why), and before training in the starter ranker form when the events are simulated. The Try it box
also marks what moved against the baseline and, under "Why is this here?", shows the signals the model leans on
most with that product's values.
