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

## Deploy in one step

    cst deploy <project> --on <local|aws|modal|gcp|azure> [--login <name>] [--yes]
    cst deploy <project> --remove

A person never sets up serving as a step of its own. `--on` names where the model runs. Captain shows one plan and
asks once, then does the work as one run with named steps:

| Step | What it does |
|---|---|
| Checking your account | Looks at what is there already |
| Creating permissions (or storage, or both) | The first time on a cloud only: what the model runs as and where its files are kept |
| Building the model's image | The first time only |
| Starting the model | Every time |
| Checking it answers | It answered. The last line printed is its address |

The second deploy on the same cloud skips the two first-time steps. On this computer there is nothing to create
and nothing to confirm.

**The plan** (`--dry-run`, or `POST /api/projects/{id}/deploy` with `dry_run`) says what will be created, in two
groups (the first time only, and for this model), what exists already, the cost by the hour and by the month, and
how long it takes. The cost is said the same way everywhere: "about $48 a month if it runs all the time", or, for a
place that sleeps, "nothing while it sleeps" and "up to" a monthly figure. The minutes come from what a real run measured on that cloud, with its date. A cloud that has
not run on a real account gives a cautious range and says it is a guess.

**It is safe to run twice.** What exists is kept. A deploy that stopped is picked up where it stopped: run the same
command again.

**When it fails midway** it says which step failed and why, lists what exists now, and offers two ways on: the same
command again (retry), or `cst deploy <project> --remove`. Remove stops the model, and takes away the first-time
work too when this project's deploy made it and no other project has a model there. A login that lacks a
permission fails its step with the sentence and the policy to hand to an admin.

**Where it can be deployed** is this computer, plus one place for each cloud login connected in Captain
(`cst cloud list`). A cloud with no login is not offered. Modal counts as a login too: `cst cloud add modal
--provider modal --auth local` says "offer Modal here", using this computer's own Modal sign-in.
`GET /api/projects/{id}/deploy/hosts` returns them for the page, each with whether it can deploy now, whether the
first-time work is still to do, whether it has run on a real account, its estimate and how long it takes. It calls
no cloud.

**For the app**: `POST /api/projects/{id}/deploy` `{host, login?, confirm?}` answers 409 with the plan until
`confirm` is sent, then `{job, run, plan}`. `run` is a live run (`GET /api/live/{run}`) whose stages are the steps
above, with the lines under each. `GET /api/projects/{id}/deploy` says where the last deploy stands (each step, the
one that failed, what exists now, `can: [retry, remove]`), and `DELETE` removes it.

### How long it takes

Measured on a real AWS account on 2026-11-12 (us-west-1, ml.t2.medium, the demo store's starter ranker beside its
baseline): one command, from an account with nothing of Captain's in it to an endpoint that answers, took
**3 minutes 50 seconds**.

| Step | Seconds |
|---|---|
| Checking your account | under 1 |
| Creating permissions (the bucket and the serving role) | 1.8 |
| Building the model's image (repository, build role, build project, the build) | 91.9 |
| Starting the model (package, register, start the endpoint) | 133.8 |
| Checking it answers (5 test requests) | 0.9 |

Right after it: 20 queries beside the baseline all answered, a median of 2.75 ms inside the endpoint, the served
features matched training on 100% of 260 checked, and the copy used 205 MB.

These numbers live in one table, `staging/timings.py`. The confirm ("About 4 minutes: about a minute and a half to
build the image and about 2 minutes to start the model"), each step of the plan (`typical_s`, `typical`) and the
lines a run prints ("Starting the endpoint (about 2 minutes)") all read it, and a test checks that the steps add
up to the confirm's total. A step that has not been timed on a real account says so instead of guessing. Moving a
running endpoint to a new version has not been timed yet.

### Which machine it runs on

A deploy runs on the smallest machine the model fits on. Nobody has to choose one.

1. **The need** is what the model takes to serve, from the same measured numbers the "this computer" card uses
   (see "Where it runs"). The demo store's ranker beside its baseline needs about 205 MB.
2. **The room**: the machine must have twice the need, and at least 1 GB more than the need.
3. **The pick** is the cheapest size with that much memory, by the price table, for that host and region.

| Host | Sizes Captain offers | The demo store gets |
|---|---|---|
| AWS | ml.t2.medium (4 GB), ml.c5.large (4 GB), ml.m5.large (8 GB), then the xlarge and 2xlarge of each | ml.t2.medium |
| Modal | 1 CPU with 1, 2 or 4 GB, then 2 CPU 8 GB, 4 CPU 16 GB, 8 CPU 32 GB | 1 CPU, 2 GB |
| Cloud Run | 1 vCPU with 512 MB, 1, 2 or 4 GB, then 2 vCPU 8 GB, 4 vCPU 16 GB, 8 vCPU 32 GB | 1 vCPU, 2 GB |
| Container Apps | 0.25 vCPU 512 MB up to 4 vCPU 8 GB, in steps of 0.25 vCPU and 512 MB | 0.75 vCPU, 1.5 GB |

Before this rule a first AWS deploy took ml.m5.large, about twice the hourly price of ml.t2.medium, whatever the
model was.

The plan says which machine and why in one line, as `machine.text` and as the last item under "For this model":
"ml.t2.medium: this model needs about 205 MB, and this has 4 GB. The smallest that leaves room to spare." Each
place in `GET .../deploy/hosts` carries the same `machine`, its price is that machine's, and `machines` lists the
sizes a person can choose from.

**A bigger machine**: `PUT /api/settings/serving` `{host, machine}`, with a name from `machines`. An empty
`machine` goes back to the smallest that fits. A choice the model doesn't fit on is refused when the deploy is
planned, with the reason. A deploy onto a place that was set up on another size changes the size first and says
so in the run ("Using ...").

**Names in the cloud account**: everything a first deploy makes starts with `captain`. `PUT /api/settings/serving`
`{name_prefix}` changes that for the workspace, for two workspaces that share an account or an account where names
must start a certain way. It is read the first time a place is set up, and does not rename what exists.

**Too big**: a model that needs more than the largest size a host has is not offered there. The place says why
("The largest Captain offers on Container Apps is 4 vCPU, 8 GB. Pick another place for it.").

`cst projects stage` and `cst compute setup serving` below are the same work in two parts, kept for agents and
scripts. They take the size they are given and do not pick one.

## Hosts: every place a staged model can run

Every host has the same shape, so staging, the request log, shadow mode, the split, the served-features check and
unstage work the same everywhere. `cst compute setup serving <host> --options` lists what one asks, and
`GET /api/serving/hosts` returns them all as cards.

| Host | What runs | Sleeps when idle | Where the bundle and the log are kept | Address and key | Last real run |
|---|---|---|---|---|---|
| `local` | A process on this computer | Stops after the idle limit | The workspace | `http://127.0.0.1:<port>`, key as a bearer token | every release |
| `aws` | A SageMaker endpoint | No: it bills by the hour until stopped | The workspace's bucket, or a serving bucket for a workspace kept on this computer | No public address: called through AWS, key in the body | 2026-11-11 |
| `modal` | A Modal web endpoint | Yes | A Modal volume | A public `modal.run` address, key as a bearer token | 2026-11-11 |
| `gcp` | A Cloud Run service | Yes | A serving bucket | A public `run.app` address, key as a bearer token | not yet |
| `azure` | A Container Apps app | Yes | A serving container in a storage account | A public address, key as a bearer token | not yet |

A host that has not run on a real account says so on its card, and Captain gives no cost estimate for it.

Each host answers the same questions:

    cst compute setup serving <host> --set name=value ...   # what it creates and costs, then a confirm
    cst compute setup serving <host> --policy               # what your login must be allowed to do, for an admin
    cst compute setup serving <host> --leftovers            # ask the cloud what of Captain's is still there
    cst compute setup serving <host> --remove               # remove exactly what setup made

The endpoint half is the host's compute provider (`compute_providers/serving.py`): deploy a bundle, status, invoke,
the host's own log, copies (fewest, most, and how long an idle one stays), the address, and removal.

**A host that sleeps when idle** stops every copy while nobody calls and starts one on the next request. Captain
never removes such an endpoint for being idle, and the runtime never stops itself there. `cst projects staging`
asks the host how many copies are up and only asks the endpoint itself when one is, so a page left open does not
keep a copy awake and billing. An endpoint with no copy up reads "Asleep: no copy is up, and it costs nothing. It
wakes on the next request." Its cost estimate is by the hour a copy is awake, and what is recorded when it stops is
a ceiling: the whole time staged, as if one copy never slept.

**Storage.** The endpoint reads its bundle and writes its log in one place, and Captain reads and writes the same
place. On Modal, Google Cloud and Azure that place is the host's own (a volume, a serving bucket), whatever the
workspace is kept on: only the serving bundle and the request log ever go there, and the endpoint's identity can
reach nothing else. The log is published into the workspace from Captain's side.

### Modal

`cst compute setup serving modal [--set environment=<env>] [--set name_prefix=captain]` makes a Modal volume and
the compute target `modal-serving`. There is no image repository and no role: Modal builds the image itself from
the installed Captain the first time a model is staged. Each staged model is one Modal app and one Modal secret,
named `<prefix>-<project>-staging`. The secret holds what the endpoint is told (which bundle, where its storage is,
the SHA-256 of the endpoint key, never the key) and is deleted with the app. Modal signs in by itself
(`modal token new`), so there is no cloud login to pick and no policy to copy.

Why a volume and not the workspace's bucket: Modal has no identity a bucket can trust, so the endpoint would need
keys. Keys short enough to be safe run out under an endpoint that stays up for weeks. A volume needs none.

Captain never deploys over, stops or deletes an app or a secret it did not make. An app of that name without
Captain's tag, or a secret of that name Captain has no record of, stops the deploy with a sentence.

Measured on a real Modal workspace, 2026-11-11, one copy of 1 CPU and 2 GB, the demo store's starter ranker beside
its keyword baseline (480 products). Every quality number from that run is SIMULATED.

| What | Measured |
|---|---|
| Setup (the volume and the target) | 1 s |
| Deploy to ready, Modal building the image | 82 to 96 s |
| Deploy to ready, image already built | 20 s |
| Cold start: first answer after it slept | 4.5 to 7 s |
| One request from a laptop 75 ms away, connection kept open | median 257 ms, 95th percentile 329 ms |
| The same on a quiet endpoint | median 212 ms, 95th percentile 234 ms |
| Ranking inside the endpoint | median 3.3 ms, 95th percentile 4.2 ms |
| Unstage, and removing the setup | 19 s, 1 s |
| Cost of the whole run (about 50 minutes, six deploys) | $0.05, from Modal's own billing summary |

What that run found and fixed:

- A Modal volume has no hard links, which is how a folder store makes a write create-only. The endpoint could not
  write its log: `PermissionError: [Errno 1] Operation not permitted`. It now opens the file create-only instead.
- A mounted volume throws away what is written and not yet committed when it reloads. A write that raced the
  reload lost its folder: `FileNotFoundError: [Errno 2] No such file or directory`. Writing, committing and
  reloading now share one lock.
- Nearly all of the response time is the network and Modal's front door, not ranking. A bare request with nothing
  to do took 168 ms from that laptop.
- Reading the request log from this computer goes through Modal's API one file at a time: a readout over about 60
  log files took 10 to 15 s. The endpoint itself is not slowed by it.
- Modal lists a stopped app for a while after it is stopped. It runs nothing and costs nothing.

Known limits: one region (Modal's default), CPU only, a container image of your own (`--image`) can't be staged
there, and Modal keeps an idle copy at most 20 minutes.

### Google Cloud (Cloud Run)

Not run on a real account yet. Tested against a stand-in of Google's API only, so its card says so and it gets no
cost estimate.

`cst compute setup serving gcp --set profile=<login> [--set region=us-central1] [--set name_prefix=captain]` makes,
in your own project: a service account the endpoint runs as, a serving bucket, an Artifact Registry repository, a
second service account the image build runs as, the image (built by Cloud Build from the installed Captain, no
Docker here), and the compute target `gcp-serving`. Each staged model is one Cloud Run service, deleted on unstage.

- **Least privilege.** Every grant is on the one bucket or the one repository. The endpoint's account may read and
  add objects in the serving bucket. The build account may read the build folder, write its log beside it, and
  push to that repository. Nothing is granted on the project.
- **APIs.** Cloud Run, Cloud Build and Artifact Registry must be on. The plan lists the ones that are off and setup
  switches them on. `--remove` leaves them on, since something else may use them by then; `--remove --set apis=off`
  switches off the ones Captain switched on. Google grants its own service agent a project role when an API is
  first switched on (Cloud Run's gets `roles/run.serviceAgent`) and keeps it when the API is switched off.
- **The address** is the service's `run.app` name, made public so a site can call it, with the endpoint key on
  every route but `/healthz` and `/ping`. Where an organization forbids public services the service stays private:
  Captain calls it with the login's own identity, and a site would need Google credentials.
- **Sleeping.** No copy is up while nobody calls. Cloud Run keeps an idle one for up to 15 minutes and decides when,
  so that time can't be set. It bills only while a copy answers, so Captain does ask the endpoint how it is.
- **What your login needs**: `--policy` prints the roles and the commands that grant them.

### Azure (Container Apps)

Not run on a real account yet. Tested against a stand-in of Azure's API only, so its card says so and it gets no
cost estimate.

`cst compute setup serving azure --set profile=<login> [--set region=eastus] [--set name_prefix=captain]` makes one
resource group, `<prefix>-serving`, and puts everything in it: a storage account with one container, a managed
identity the endpoint runs as, a Basic container registry, the image (built by the registry itself, no Docker
here), a Container Apps environment on the Consumption plan, and the compute target `azure-serving`. Each staged
model is one container app in that group, deleted on unstage. Removing the setup deletes the group, and is refused
if the group holds anything Captain did not make.

- **Least privilege.** The identity may read and write that one container and pull that one image. The login that
  runs setup is let into the container too: in Azure, owning a storage account does not let you read its blobs.
  All three grants are inside the group. The storage account takes no keys, only signed-in identities.
- **Cost while idle.** The registry costs about $0.17 a day for as long as the setup exists. The endpoint costs
  nothing once it has slept. Container Apps costs more in some regions (West US 2 is 42% above East US on CPU).
- **What your login needs**: Contributor and Role Based Access Control Administrator on the subscription, since
  setup makes its own group. Owner has both. `--policy` prints them.
- **Kept from the first Azure job run**: the image carries Azure's sign-in library, the container is told which
  identity to use (`AZURE_CLIENT_ID`), and the copy's size is set on the app itself.
- **Known limits.** Setup makes no log workspace, so the app's own lines are read with `az containerapp logs show`.
  An idle copy is taken away after about 5 minutes, and Captain can't change that yet. A container app's name is
  at most 32 characters, so a long one keeps its start and gains a short hash.

### Copies

    cst projects staging-copies <project>                       # the fewest, the most, how long an idle one stays
    cst projects staging-copies <project> --fewest 1 --most 3   # change them: the host applies it, no restage

`--fewest 1` keeps one copy up, so no request waits for a cold start, and it bills while it is up. On Modal
`--idle-seconds` sets how long an idle copy stays (2 to 1200). The app reads and sets the same through
`GET` and `POST /api/projects/{id}/staging/scale`. On this computer there is one copy.

**On SageMaker** an endpoint never sleeps, so the fewest is at least 1 and each copy bills by the hour. With the
most above the fewest, Captain turns on SageMaker's own scaling: it adds a copy when one copy takes more than
`requests_per_copy` requests a minute (3,000 unless the compute target says otherwise), waits 60 seconds before
adding another, and 5 minutes before taking one away. A new copy takes a few minutes to arrive, so this follows a
rise over minutes, not a spike. The target's `min_copies`, `max_copies` and `requests_per_copy` are what a deploy
starts with. `staging-copies` changes a running endpoint, and a redeploy goes back to the target's settings.
Written against a stand-in. Not run on a real account yet.

A redeploy onto an endpoint that load has scaled out starts the new version on as many copies as are up, so a busy
endpoint is not cut back in the middle of the switch. Scaling is taken off for the move and put back after it,
because SageMaker does not move an endpoint while its scaling is on.

The login needs a few more permissions for this (`MoreThanOneCopy` and `ScalingServiceRole` in the policy
`cst compute setup serving aws --policy` prints). Without them the model still starts, on its fewest copies, and
the run says scaling could not be turned on. The first time scaling is used in an account, AWS itself creates one
role there, `AWSServiceRoleForApplicationAutoScaling_SageMakerEndpoint`. It is AWS's, shared by every endpoint in
the account, and Captain does not remove it.

**What does not depend on one copy** (tested with two copies over one store):

- The request log. Each copy writes its own chunks under the same folder with create-only writes, so two copies
  never overwrite each other. Each request row says which copy answered it (`copy`).
- The split. A session's version is a hash of the experiment's salt and the session id, so every copy gives the
  same session the same version.
- Shadow mode. Every copy serves the baseline and logs the candidate's answer beside it.
- Stopping when idle. A copy that has had no request for the idle limit asks the other copies when they were last
  used before it stops the endpoint. Each copy leaves that in `staging/<project>/<env>/copies/<deployment>/`.
  Before this, one quiet copy could delete an endpoint another copy was busy on.

**What is per copy.** A change of traffic (the split, shadow mode, the idle limit) reaches the copy that was told at
once and the others within 3 seconds. `flush` and `status` reach one copy, whichever the host picks, so the newest
15 seconds of another copy's requests can be missing from a readout taken right after a flush.

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

## Which requests count as shoppers

A request counts as a real shopper only when the site says so: it sends `"traffic": "real"`, as the code under
Connect your site does. A request with no label is **Unlabelled**. It may be a shopper, a script or this computer,
and Captain doesn't guess. Unlabelled requests are answered and logged like any other, but they don't open the A/B
form, they are never REAL_TRAFFIC evidence, and a readout made of them can't go to launch review. `simulated` is the
shopper simulator and `test` is the console and test requests.

## The deployment page, tab by tab

A staged model has its own page. The header carries the address, the key, a test request, where it runs, and a
chart of requests over a time range you pick (15 minutes, 1 hour, 24 hours, 7 days), with errors marked in red and
a thin line where each version was staged. A small card stays in view on every tab and says which of three states
the endpoint is in: **stopped**, **idle** (up, ready, nothing to do, which is fine), or **handling requests**.

| Tab | What it shows | From the terminal |
|---|---|---|
| Requests | Newest first. A row opens beside the list: the results, the signals served, both response times, and that request's own log lines. | |
| Try it | A query against both versions. | `cst projects staging-query` |
| Traffic | Connect your site, shadow mode, the A/B test. | `staging-connect`, `shadow`, `ab-*` |
| Copies | The running copy: where, version, startup time, requests handled, peak memory, its state. Staging runs one copy, and none when idle. | |
| Metrics | Requests, response time (median and 95th percentile), errors, copies, memory, requests being answered. | |
| Versions | Every deploy, the live one marked, Roll back on recent ones. | `cst deployments history`, `rollback` |
| Logs | Lines to the millisecond with a filter. Lines of requests handled at the same time can interleave. New lines arrive within about 15 seconds. | `cst deployments logs [-f] [--filter] [--request]` |
| Settings | Where it runs, the machine, copies, stopping when idle, time limits, the served-features check. The idle time can be changed while it runs. | |
| Cost | So far this month, the last 24 hours, the rate while it is up, by machine. Worked out from uptime at the list price. | |

**Two response times.** "At the endpoint" is the time the ranking took inside it. Captain measures it on every
request. "As your site sees it" includes the network, and only the site can know it, so the site reports it:
`client_ms` with an outcome, or the event `timing` with `ms`. The Connect your site code does this. Measured on
2026-11-11 from a laptop to a SageMaker endpoint in us-west-1: a median of 0.2 ms at the endpoint and 111 ms as the
caller saw it. The network is nearly all of it, which is why both are shown.

**What to expect when staging in AWS** (measured twice on 2026-11-11, ml.t2.medium, us-west-1): building the
serving image in your account takes about a minute and a half (78 seconds), once. An endpoint is ready about two
minutes after you confirm (115 and 126 seconds).

## The same thing from the terminal

```
cst deploy <project> [--target local] [--yes]     stage the candidate. The last line printed is the address.
cst deployments list [--stopped | --all]          state, copies, where and since when, across projects
cst deployments history <project>                 each version: number, when, who, model version
cst deployments logs <project> [-f]               the endpoint's log
cst deployments stop <project>                    stop it and remove what Captain created
cst deployments rollback <project> [version]      put an earlier version back (default: the one before the live one)
```

`cst projects stage`, `staging` and `unstage` do the same and stay. `cst deploy promote|rollback|status|history`
are the gated environment commands and are unchanged: a project that has one of those names is staged with
`cst deploy to-staging <project>`.
