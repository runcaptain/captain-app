# Staging: a model version as a live endpoint, in your own cloud

Staging runs one model version as a live endpoint you can query, beside the project's baseline. It runs on your
compute target (a SageMaker endpoint in your AWS account) or, with `--target local`, as a process on this computer.
Captain hosts nothing and never deploys to production: launch review is still the gate before that.

    cst projects stage <project> [--candidate <id>|--baseline] [--env staging] [--target <compute target>|local]
    cst projects staging <project>                  # what runs, where, since when, and when it stops by itself
    cst projects staging-query <project> "<query>"  # the staged results beside the baseline's
    cst projects staging-check <project>            # did serving compute the features training computes?
    cst projects ab-start <project> --split 0.5     # an A/B test between the baseline and the candidate
    cst projects ab <project>                       # the readout
    cst projects ab-ramp <project> 0.25             # change the candidate's share; ab-rollback sends everyone back
    cst projects unstage <project>                  # stop it and remove everything Captain created

The app does the same from the project page (Ship: 1 Stage, 2 Try it, 3 A/B test, 4 Launch review), through
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

## Cost, and stopping by itself

Before any cloud deploy Captain shows the instance type, an hourly estimate (US list price: your region and
discounts change it) and "This keeps billing until you stop it", and starts nothing without a confirm (`--yes`).

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

**Ramp and roll back** are one call each and are written to the project's history. Rolling back during a test
sends every shopper to the baseline again and ends the test. With no test running, it restages the version that
was on the endpoint before this one.

Launch review stays the gate before production. Captain does not deploy to a production endpoint.
