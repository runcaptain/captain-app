# Deploying: a model version as a live endpoint, in your own cloud

Deploying runs one model version as a live endpoint you can query, beside the project's baseline. It runs on your
compute target (a SageMaker endpoint in your AWS account) or, with `--target local`, as a process on this computer.
Captain hosts nothing and never deploys to production: launch review is still the gate before that.

    cst projects stage <project> [--candidate <id>|--baseline] [--env staging] [--target <compute target>|local]
    cst projects staging <project>                  # what runs, where, since when, and when it stops by itself
    cst projects staging-query <project> "<query>"  # the staged results beside the baseline's
    cst projects staging-check <project>            # did serving compute the features training computes?
    cst projects staging-connect <project>          # the code a site pastes, an address a model, and real requests so far
    cst projects shadow-start <project>             # shoppers keep the baseline; the candidate ranks silently
    cst projects shadow <project>                   # what the candidate would have changed
    cst projects unstage <project>                  # stop it and remove everything Captain created

The app does the same from the project page (Ship: 1 Deploy, 2 Try it, 3 Use it from your site),
through `POST /api/projects/{id}/stage`, `/unstage`, `/staging/query`, `/staging/check` and
`GET /api/projects/{id}/staging`.

**Captain runs no A/B tests.** It does not assign shoppers to models and does not collect what they click or buy.
To compare models on real shoppers, split traffic in your own testing tool and send each group to its model's
address (see "Use it from your site"). The old `cst projects ab-*` commands print one line saying so and exit 2.
A record of a test left in a project by an older version is ignored, and nothing about a test can stop a deploy.

## What can be deployed

| The version | What happens |
|---|---|
| A keyword-search version, or a starter ranker (`local-bm25`, `bm25-ltr`) | Captain's runtime serves it: keyword search finds the candidates, the trained model reorders the top K with features from the module that built its training set |
| A version that ranks by calling a service you run (`http`, `elastic`, ...) | Wrapped: Captain's runtime sits in front of your service and does the logging |
| A model trained elsewhere with no serving entry point | Refused in a sentence. Bring `--endpoint <url>` (any service that answers `POST {query, k}` with `{ids}`) or `--image <uri>` (a container that follows SageMaker's serving contract) |

`--endpoint` runs Captain's runtime on this computer in front of your service. `--image` deploys your container to
the SageMaker target as it is: Captain can query it, but it logs nothing.

## The serving bundle

`stage` writes a bundle to the workspace's storage, `staging/<project>/bundles/<id>/`: `bundle.json` names each arm
(baseline, candidate) with its model version and files, and the artifact files (the trained model, its feature spec
and lookup tables) are copied beside it. The catalog is stored once per version under `staging/<project>/catalogs/`.
A local endpoint and a cloud endpoint load the same bundle, so what you try here is what the cloud serves.

## The endpoint

| Route | What it does |
|---|---|
| `POST /rank` | `{query, k?, user_id?, session_id?, request_id?, traffic?}` gives `{request_id, ids, scores, arm, model_version, ms}` |
| `POST /rank/<model>` | The same from one named model, `baseline` or `candidate`: the address your own testing tool gives a group of shoppers |
| `POST /timing` | `{request_id, ms}`: how long the request took as your site saw it. Optional, and the only thing a site reports back |
| `POST /flush` | Write buffered log records now. `{"publish": true}` also publishes the logs dataset |
| `GET /healthz` | What is loaded, uptime, requests served, seconds until it stops by itself. No secrets |

Every route but `/ping` and `/healthz` needs the endpoint key: `Authorization: Bearer <key>` on a local endpoint.
SageMaker exposes one route, so there the body carries it: `{"op": "rank", "key": "<key>", "query": ...}` to
`InvokeEndpoint` (ops: `rank`, `timing`, `flush`, `status`, `reload`). The key is generated when you deploy and kept
in this computer's keychain as `STAGING_<PROJECT>_<ENV>_KEY`; the endpoint only ever holds its SHA-256. On a machine
with no keychain, set that name in the environment before deploying and that value is used. A local endpoint binds to
127.0.0.1 only.

`traffic` says where a request came from: `real`, `test`, or nothing (unlabelled). The Try it box sends `test`.
Only `real` ever counts as REAL_TRAFFIC evidence.

## Where it runs: this computer, or your cloud

Before a deploy, Captain offers the places a model can run with what each costs: this computer (free, only you can
reach it) and the connected cloud (an hourly estimate, a site can reach it). The last choice is remembered per
project.

**A big model is deployed to a cloud, not on this computer.** There is a size limit for deploying here, separate from
free memory: by default a model that needs more than 1 GB to serve (the estimate below, with headroom, counting the
baseline beside it) is not offered on this computer, even when the memory is free. The choice is switched off with
the reason and a link to the setting (Settings, Costs: "Largest model to deploy on this computer": 250 MB, 500 MB,
1 GB, 2 GB, 4 GB or No limit). `cst projects stage --target local` refuses the same way unless `--allow-large` is
passed. With a cloud connected the cloud is offered first.

**This computer is only offered when the model fits.** A local endpoint holds the catalog, a keyword index for
each version and a trained ranker's feature tables in one process. Captain estimates that from the catalog's size
and compares it with the memory and disk free right now. Measured 2026-11-11 on demo-shaped catalogs: about 8.0 KB
a product for a trained ranker, 4.5 KB a product for a keyword version, 0.45 KB a product for the rows, 120 MB for
the process, plus 30% headroom. A 200,000 product catalog with both versions needs about 3.4 GB, loads in about 7
seconds, and answers a query in about 120 ms. If it won't fit, the choice is shown switched off with the reason
("This catalog needs about 9 GB of memory to serve, and this computer has 3 GB free. Deploy it to a cloud."). If
it is tight, it is allowed with a warning. A deploy is refused before anything starts when it won't fit, and an
endpoint that runs out of memory while loading says so.

**Set up serving in your AWS account** (`cst compute setup aws-serving --profile <login> --region <region>`, or
the button on the deployment page) creates what a SageMaker serving target needs, after showing exactly what and
a confirm: the role the endpoint runs as (it can read the serving bundle, write the request log, pull its image and
delete its own endpoint, and nothing else), a repository for the serving image, the image, and the compute target.
The image is built inside your account by AWS CodeBuild from the installed Captain (measured: 78 seconds, about
$0.01),
so no Docker is needed on your computer. Everything is named `<prefix>-serving...` and tagged `app=captain`.
`--remove` takes it all away again and is refused while a model is deployed there. `--policy` prints the exact
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

### What a request costs in time

A request from this computer to a cloud endpoint has two very different costs, so a query reports them apart
(`timing` in `POST .../staging/query`, and the last line `cst projects staging-query` prints):

- **The first call** from a process signs in to the cloud and opens a connection. It can take several times as
  long as the request itself. `timing.first_call_ms`, null once this process is connected.
- **A warm call**, with the connection open: `timing.warm_ms`, of which `timing.inside_ms` is the model.

Before 2026-11-12 Captain made a new cloud client for every call, so every call paid the first cost: one query
from the terminal showed round trips of 848 ms and 480 ms against 3.9 ms and 1.4 ms inside the endpoint. Measured
on this laptop, building a client alone took 50 to 70 ms each time, loading the AWS library 135 to 190 ms once,
and each new client opened a new connection. Captain now keeps one sign-in and one client per login and service
for the life of the process (30 minutes at most). The round trip also no longer counts the time this computer
takes to look up product titles for the answer. A site that calls the endpoint should do the same: make one
client and keep it.

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

## Hosts: every place a deployed model can run

Every host has the same shape, so deploying, the request log, the quiet test, the served-features check and
stopping work the same everywhere. `cst compute setup serving <host> --options` lists what one asks, and
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
a ceiling: the whole time deployed, as if one copy never slept.

**Storage.** The endpoint reads its bundle and writes its log in one place, and Captain reads and writes the same
place. On Modal, Google Cloud and Azure that place is the host's own (a volume, a serving bucket), whatever the
workspace is kept on: only the serving bundle and the request log ever go there, and the endpoint's identity can
reach nothing else. The log is published into the workspace from Captain's side.

### Modal

`cst compute setup serving modal [--set environment=<env>] [--set name_prefix=captain]` makes a Modal volume and
the compute target `modal-serving`. There is no image repository and no role: Modal builds the image itself from
the installed Captain the first time a model is deployed. Each deployed model is one Modal app and one Modal secret,
named `<prefix>-<project>`. The secret holds what the endpoint is told (which bundle, where its storage is,
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
| Stop, and removing the setup | 19 s, 1 s |
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

Known limits: one region (Modal's default), CPU only, a container image of your own (`--image`) can't be deployed
there, and Modal keeps an idle copy at most 20 minutes.

### Google Cloud (Cloud Run)

Not run on a real account yet. Tested against a stand-in of Google's API only, so its card says so and it gets no
cost estimate.

`cst compute setup serving gcp --set profile=<login> [--set region=us-central1] [--set name_prefix=captain]` makes,
in your own project: a service account the endpoint runs as, a serving bucket, an Artifact Registry repository, a
second service account the image build runs as, the image (built by Cloud Build from the installed Captain, no
Docker here), and the compute target `gcp-serving`. Each deployed model is one Cloud Run service, deleted when you stop it.

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
here), a Container Apps environment on the Consumption plan, and the compute target `azure-serving`. Each deployed
model is one container app in that group, deleted when you stop it. Removing the setup deletes the group, and is refused
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
- Each model's own address. `/rank/candidate` and `/rank/baseline` give that model from whichever copy the host
  hands the request to, so a team that splits traffic in its own system gets the same answer from every copy.
- Shadow mode. Every copy serves the baseline and logs the candidate's answer beside it.
- Stopping when idle. A copy that has had no request for the idle limit asks the other copies when they were last
  used before it stops the endpoint. Each copy leaves that in `staging/<project>/<env>/copies/<deployment>/`.
  Before this, one quiet copy could delete an endpoint another copy was busy on.

**What is per copy.** A change of traffic (the quiet test, the idle limit) reaches the copy that was told at
once and the others within 3 seconds. `flush` and `status` reach one copy, whichever the host picks, so the newest
15 seconds of another copy's requests can be missing from a readout taken right after a flush.

## What is deployed: the cloud is the truth

Captain keeps a record of each deployment, and a record can be wrong: an endpoint deleted in the cloud's own
console, a deploy whose process died, a model another workspace deployed into the same account. So "what is
deployed" is answered by asking each connected cloud, and Captain's records are corrected from the answer.

`GET /api/deployed` (in Python `staging.fleet.deployed(ctx)`) returns one row per deployed model across projects
and clouds: the model version, the project, the cloud, region, machine and copies, the state as the cloud reports
it (starting, running, asleep, stopped, failed with the cloud's reason), since when and who deployed it. Each row
also says how the record and the cloud line up (`link`):

| `link` | Means |
|---|---|
| `linked` | A project's record and the thing in the cloud, matched. |
| `unlinked` | In the cloud, made by Captain, and no project record knows it: "Found in your AWS account, not linked to a project." |
| `gone` | The record said running and the cloud does not have it: "Removed outside Captain." |
| `local` | On this computer. There is no cloud to ask, so the process is checked. |
| `unknown` | The cloud could not be asked just now. The record is shown as it stands and nothing is corrected. |

What is Captain's in a cloud account is found two ways: by Captain's tag (`app=captain`), and by the name prefix
the workspace's cloud resources start with. Something that has neither is not Captain's and is never listed.

**Corrected on every read.** A record marked failed, stopped or starting whose endpoint the cloud reports running
is set to running. A record marked running whose endpoint is gone is set to stopped, "removed outside Captain". A
record marked running whose endpoint the cloud reports failed is set to failed, with the cloud's reason. A deploy
left failed or stalled for a project whose model is running is closed as done. Each row lists what was just
corrected, in sentences (`corrected`), and `GET /api/deployed/differences` prints only that.

Why: on 2026-11-12 a project page showed "Deploying to AWS stopped. Starting the model: failed" beside an endpoint
that was in service and answering. Two causes, both fixed. A deploy that printed nothing for three minutes was
taken to have died, and a cloud starting an endpoint prints nothing while it waits: a running deploy now says it
is alive every 30 seconds. And nothing cleared a failed deploy when the model came up: the project's deploy status
now asks the cloud before it says failed.

**What it costs.** A few read-only calls to each cloud's own listing. It never sends a request to a model, so a
host that sleeps when idle is not woken. The answer is kept for 5 seconds, so a screen that redraws does not ask
again, and never longer.

**Not run on a real account yet.** Tested against each host's stand-in: running, removed outside Captain, found
with no project, failed, a stale failed deploy beside a healthy endpoint, and a cloud that cannot be asked.

### The Deployments screen

The screen (`defaults/mods/captain-deployments`, the sidebar's Deployments) is the place to manage deployed models.
It reads `GET /api/deployed`, so it shows what the clouds report, never Captain's records alone.

- **One row per deployed model**, across every project and cloud: the cloud's mark, the project and the model
  version, the state in words as the cloud reports it, where it runs (cloud, region, machine), a small chart of
  the last hour's requests, the requests and failures of the last 24 hours, the typical response time, the cost an
  hour and this month, and who deployed it and when. What was just corrected from the cloud is said on the row.
- **Buttons on a row**: Open, Stop and "Go back to an earlier version" on a running model. Open, "Start again"
  and "Remove from the cloud" on a stopped one. "Link to a project" and "Remove from the cloud" on something found
  in a cloud with no project. Starting, removing and removing a cloud's setup ask first, and the question holds
  the screen's one filled button.
- **Filters**: Running, Stopped, All. By cloud and by project when there is more than one.
- **What Captain created in your accounts**: what the first deploy to each cloud made, with what each thing is
  for, and "Remove everything" when no model is deployed there.
- **A model's page** (Open): its state, its address or its name in the cloud with Copy, then two parts, "Traffic and cost"
  and "Details". Traffic and cost has the charts at full size over 15 minutes, 1 hour, 24 hours or 7 days: requests a minute, requests
  that failed, response time, processor, memory and copies, and what it costs now, today and this month. Details
  has the requests it answered, its log, the earlier versions with "Put this version back", and its copies
  and machine.

A cloud that could not be asked is said above the list, and nothing can be done to a deployment Captain could not
check. The picture check draws every state from sample rows handed to the screen (`props.demo`,
`tests/mod-render/deployments-demo.py`), so nothing is deployed to take a picture.

### Something in the cloud that no project knows

A row whose `link` is `unlinked` is something Captain made that this workspace has no record of: another
workspace deployed it into the same account, or its record was lost. Two things can be done with it.

- **Link it to a project** (`POST /api/deployed/link {id, project}`, `cst deployments link <id> <project>`). It
  becomes that project's deployment, and can be watched, stopped and removed from there. Captain does not know
  which model version it runs, or the key it was made with, and says so: to ask it for results, deploy from the
  project again, or use its own key. A workspace that has never set that cloud up has nowhere to keep it, and is
  told to deploy there once first.
- **Remove it** (`POST /api/deployed/remove {id, confirm}`, `cst deployments remove <id> --yes`). Without the
  confirm it says what would be deleted. On AWS the endpoint goes with its endpoint config and its model, so
  nothing is left billing or lying about. It cannot be undone.

### From the terminal

    cst deployments list [--stopped | --all] [--fresh]   # every deployed model, as each cloud reports it
    cst deployments show <project> [--range 1h]         # state, where, traffic, machine, cost
    cst deployments sync                                # correct Captain's records, and print what differed
    cst deployments stop <project>
    cst deployments start <project> --yes               # the same model version, in the same place
    cst deployments rollback <project> [version]
    cst deployments remove <project> --yes              # stop it and remove what Captain made for it
    cst deployments link <id> <project>                 # something found in a cloud with no project

Each takes `--json` before the command (`cst --json deployments list`) for an agent to read.

## Traffic and numbers, from the cloud's own metrics

`GET /api/projects/{id}/deployed/numbers?range=1h` (in Python `staging.numbers.numbers`) gives one deployment's
requests a minute, errors, response time at the endpoint (median and 95th percentile), CPU and memory, the copies
that were up, and its cost: the hourly rate, today and this month. Ranges: 15 minutes, 1 hour, 24 hours, 7 days.

| Host | Requests, errors, response time | CPU, memory, copies over time | Cost today and this month |
|---|---|---|---|
| AWS | CloudWatch | CloudWatch for processor and memory, when AWS has published them (it does not always). Copies up now from AWS's own count | Captain's own record of when it was up, at the list price |
| Modal | The request log | Not read yet. Copies up now come from Modal | Captain's own record of when it was up |
| Google Cloud | The request log | Not read yet | Captain's own record |
| Azure | The request log | Not read yet | Captain's own record |
| This computer | The request log | Not read | Nothing |

What a host does not report is said in a sentence (`not_read`). It is never shown as a zero.

**What AWS has, and what it does not, as found on a real account on 2026-11-12.** For an endpoint that ran for an
hour on ml.t2.medium, AWS held every request number (requests, failures, response time), by endpoint name and
variant. It held no processor or memory numbers at all: they were never published for it. So:

- Requests, failures and response time are read from AWS. Response time arrives in microseconds and is shown in
  milliseconds.
- Processor and memory are shown when AWS has them. When it has none, the answer says "AWS has not published
  processor and memory numbers for this endpoint.", and no empty chart is drawn.
- Hours and cost come from Captain's own record of when the model was up, at the list price for its machine:
  its ledger of finished runs, or the start and stop times when the ledger holds nothing. It is an estimate, and
  the AWS bill is the final word. The hours were first counted from the processor reports each copy sends. With
  none sent, an hour of running counted as nothing.
- Copies are the number AWS says are up now, and the fewest and most it may run, in words. There is no chart of
  copies over time, because no history of them is kept that can be relied on.

**What counts as a request.** AWS counts every call to the endpoint, and Captain makes calls of its own: it asks
the model whether it is well, and asks it to write its log. On a real endpoint on 2026-11-12, 24 test searches read
as "160 requests in the last hour", because those calls were most of the count. The request log holds only
requests for results, each with its own time. So when the log has any, requests, failures and response time come
from it, and AWS's figure is shown beside them as what it is: calls that reached the endpoint, Captain's checks
included. With no request log (a model that is not Captain's own runtime), AWS's count stands and says so.

The 95th percentile is asked of AWS over the whole range, as one figure. The slowest minute's 95th is not the
range's, and was shown as if it were. CPU is a share of one core: 100% is one core in full use.

**What reading costs.** CloudWatch charges about a cent for a thousand metrics read, and each look reads ten.
A cloud's numbers are counted by the minute and arrive a minute or two late, so asking more often buys nothing.
The answer for a deployment and range is kept for one minute. The cost asks AWS nothing. A screen left open on
one deployment all day reads about 14,000 metrics, about 14 cents.

Every call is read-only, and none is sent to the model, so a sleeping host stays asleep.

**Run on a real account once, 2026-11-12,** for a stopped endpoint: the state, the request numbers and the
missing processor numbers above are from that read. The charts of a running endpoint under real traffic have not
been checked against the CloudWatch console yet.

## Cost, and stopping by itself

Before any cloud deploy Captain shows the instance type, an hourly estimate and "This keeps billing until you stop
it", and starts nothing without a confirm (`--yes`). The estimate is the shared one every form uses
(`POST /api/costs/estimate`, kind `staging`): the live AWS price for the target's region when it can be read, else
the bundled table with its date. When an endpoint is stopped, what it cost while it was up is recorded on the
project and counts in the project's running total.

An endpoint stops by itself after 240 minutes with no `/rank` (`--idle-minutes` changes it). A local
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
serving resources", `captain` unless set), each tagged `app=captain`, `project=<project>`, `env=<env>`. They are
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

A model deployed on this computer writes the same log to the local workspace store.

## The request log

Every request is logged with its request id, timestamp, query, the ids returned in order, the feature row the model
was given for each of the first 10 results, the model version, the arm and the latency. Records are buffered, written
as write-once chunks under `staging/<project>/<env>/logs/<stage>/` every 15 seconds, and published as a new version
of the dataset `<project>/staging-logs` every 5 minutes (and when the endpoint stops). The dataset has one row per
result served: `query, product_id, position, session_id, ts, request_id, arm, model_version, traffic, ms, features`.
It is a log of what was served, with nothing about what a shopper did. A version that holds anything but real traffic is
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

## Use it from your site

`cst projects staging-connect <project>` (step 3 on the page) says where the model is and who can reach it, gives
the code a site pastes, the address of each model, and counts the real requests that have arrived: "Waiting for
the first request", then "Received 12 requests, the last one 4 s ago". Only requests marked `real` count. The Try
it box and `staging-test` are not a site.

**A model on this computer answers on 127.0.0.1 only. No site can reach it.** Deploy it to a cloud for that.

**The one call** (`--code curl|javascript|python`, or `aws cli` for SageMaker):
`POST /rank {query, k, session_id, traffic: "real"}` returns `{request_id, ids, scores, arm, model_version, ms}`.
The code can also tell Captain how long the request took as your site saw it (`POST /timing {request_id, ms}`), so
the response time on the Metrics chart includes the network. It sends nothing about what the shopper did.

**An address a model.** To compare models on real shoppers, split traffic in your own testing tool and send each
group to its model's address:

| Model | Address |
|---|---|
| The deployed model | `<address>/rank` |
| The compared model | `<address>/rank/baseline` |

On SageMaker there is one endpoint: send `"op": "rank"`, and `"arm": "baseline"` for the compared model. What
shoppers click or buy stays in your tool. A site that still posts to `/outcome` gets 410 and a sentence.

**Your site must fall back to its own search.** The code gives the endpoint 300 ms. If it doesn't answer in
time, is unreachable, or answers with an error, show your own results. An error is a 4xx or 5xx status with the
body `{"detail": "<a sentence>"}`. A slow or missing ranking must never hold up the page.

**The key.** The code reads it from `CAPTAIN_STAGING_KEY` in your site's own environment, so it is never in
your code. The app's "Copy the key" button puts it on the clipboard. It is never shown on screen or written to a
log.

`cst projects staging-test <project>` sends one request through the same door, marked as test traffic, to prove
the path.

## Test quietly with a copy of your site's requests

    cst projects shadow-start <project>
    cst projects shadow <project> [--traffic real|test]
    cst projects shadow-stop <project>

The endpoint answers every request with the compared model's ranking, exactly as before, and this model ranks a
copy of the same request silently. Both rankings are logged. No shopper sees this model, nobody is assigned to
anything, nothing a shopper does is collected, and a model that fails never changes the answer the site gets. The
readout says how many requests there were, how often the top result and the top 10 would have differed, the
average overlap of the top 10, the queries that differ most, and both response times. It reads REAL_TRAFFIC only
when the requests were real. It ends when the model stops. (The commands and the API still call it `shadow`.)

## Launch review, and where its evidence comes from

A launch needs HUMAN_GRADED or REAL_TRAFFIC evidence. That rule is unchanged. Captain no longer produces
REAL_TRAFFIC evidence itself, because it runs no A/B tests: it comes from your own testing tool, and you bring the
result in on the launch review (`cst handoff outcome <id> --metric ... --value ... --source "<the tool and its
report>"`). It is recorded with where it came from. Launch review is on no screen in the app: it runs from the
terminal (`cst handoff`), for a team that wants a recorded check before production.

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
Use it from your site does. A request with no label is **Unlabelled**. It may be a shopper, a script or this computer,
and Captain doesn't guess. Unlabelled requests are answered and logged like any other, but they are never
REAL_TRAFFIC evidence. `test` is the console and test requests. `simulated` appears only in logs written by older
versions.

## The deployment page, tab by tab

A deployed model has its own page. The header carries the address, the key, a test request, where it runs, and a
chart of requests over a time range you pick (15 minutes, 1 hour, 24 hours, 7 days), with errors marked in red and
a thin line where each version was deployed. A small card stays in view on every tab and says which of three states
the endpoint is in: **stopped**, **idle** (up, ready, nothing to do, which is fine), or **handling requests**.

| Tab | What it shows | From the terminal |
|---|---|---|
| Requests | Newest first. A row opens beside the list: the results, the signals served, both response times, and that request's own log lines. | |
| Try it | A query against both versions. | `cst projects staging-query` |
| Use it from your site | The code a site pastes, an address a model, the quiet test. | `staging-connect`, `shadow` |
| Copies | The running copy: where, version, startup time, requests handled, peak memory, its state. On this computer it runs one copy, and none when idle. | |
| Metrics | Requests, response time (median and 95th percentile), errors, copies, memory, requests being answered. | |
| Versions | Every deploy, the live one marked, Roll back on recent ones. | `cst deployments history`, `rollback` |
| Logs | Lines to the millisecond with a filter. Lines of requests handled at the same time can interleave. New lines arrive within about 15 seconds. | `cst deployments logs [-f] [--filter] [--request]` |
| Settings | Where it runs, the machine, copies, stopping when idle, time limits, the served-features check. The idle time can be changed while it runs. | |
| Cost | So far this month, the last 24 hours, the rate while it is up, by machine. Worked out from uptime at the list price. | |

**Two response times.** "At the endpoint" is the time the ranking took inside it. Captain measures it on every
request. "As your site sees it" includes the network, and only the site can know it, so the site reports it:
`POST /timing` with the request id and `ms`. The code under Use it from your site does this. Measured on
2026-11-11 from a laptop to a SageMaker endpoint in us-west-1: a median of 0.2 ms at the endpoint and 111 ms as the
caller saw it. The network is nearly all of it, which is why both are shown.

**What to expect when deploying to AWS** (measured twice on 2026-11-11, ml.t2.medium, us-west-1): building the
serving image in your account takes about a minute and a half (78 seconds), once. An endpoint is ready about two
minutes after you confirm (115 and 126 seconds).

## The same thing from the terminal

```
cst deploy <project> [--target local] [--yes]     deploy the candidate. The last line printed is the address.
cst deployments list [--stopped | --all]          state, copies, where and since when, across projects
cst deployments history <project>                 each version: number, when, who, model version
cst deployments logs <project> [-f]               the endpoint's log
cst deployments stop <project>                    stop it and remove what Captain created
cst deployments rollback <project> [version]      put an earlier version back (default: the one before the live one)
```

`cst projects stage`, `staging` and `unstage` do the same and stay. `cst deploy promote|rollback|status|history`
are the gated environment commands and are unchanged: a project that has one of those names is deployed with
`cst deploy to-staging <project>`.

## Health, and being told when it changes

Captain keeps a deployed model's health as a fact on its record, and a file for every check it ran.

**What a check asks.** Is it answering (one status call, five seconds at most). How many of the last 15 minutes'
requests failed. How long 95 in 100 took at the endpoint. When the last request was.

**What healthy means** is yours to set: `cst deployments health <project> --p95-ms 200 --error-rate 0.01`, or
"Is it healthy?" on the deployed model's screen. Until you set one, a model is unhealthy when it stops answering
or fails more than 5 in 100 requests, and response time is not judged. With fewer than 20 requests in the window
the rates are not judged at all, and the health says so.

**When it runs.** Once a minute while the app's service is open. `cst deployments watch` does the same for an
agent or a server, and `cst deployments watch --once` checks once and exits 1 when anything is unhealthy, for a
cron. `cst deployments health <project> --check` runs one now.

**Unhealthy takes two bad checks in a row**, so one slow minute is not an alert. Recovering takes one good one.
On either change a message goes out, once:
- to the webhook you set (`cst deployments alerts --webhook https://...`): a JSON body with `text`, `project`,
  `model_version`, `status`, `reasons` and `at`, which Slack and PagerDuty accept. The address can hold a secret:
  it is never printed or sent to a screen, only whether one is set;
- as a notification of the computer the service runs on (macOS), and in the app.

**Captain's own calls to a model in a cloud are rare.** On SageMaker every call to an endpoint counts as traffic:
it shows in your own metrics, can trip an alarm on invocations, and counts toward adding a copy. So whether a
model is up comes from the cloud's own state, which costs nothing to ask. A page that is open makes no call to
a model in a cloud, however long it stays open. The model itself is called only when you ask for something that
needs it: a search, "Check now", a test request, a refresh of the requests or the log. Those calls are not
requests for results, so they are not in the request log or its counts. The scheduled health check never calls
a model in a cloud. It reads the cloud's own state for "is it up" and the request log for "does it answer well".
With no requests in the window it says "Running. No requests to judge by yet.", which is neither healthy nor
unhealthy and is not an alert. The health panel says where each fact came from. A model on this computer is asked every time: nothing counts it.

**What a check never does.** It never wakes a host that sleeps when idle: with no copy up the health is "Asleep".
It never alerts while a deploy is running on that project. It never calls a model healthy on numbers it doesn't
have.

`GET /api/projects/<id>/staging/health[?history=N]`, `POST .../health/check`, `PUT .../health/target`, and
`GET /api/alerts` for every unhealthy model in the workspace.

## Rolling back

A roll back is a deploy of an earlier version to the same place, under the same login, through the same job
every deploy runs. So it works on every host a deploy works on, and runs the same steps.

- `cst deployments rollback <project> --reason "..."` puts back the version before the one running.
  Add a number from `cst deployments history <project>` to pick another. A reason is required.
- **What is running keeps answering during the switch.** The earlier version is started and checked first, and
  only then is the other one stopped. If the earlier version fails to start, what was running stays up.
- **The key stays the same** across a redeploy and a roll back, so a site's key keeps working.
- **Who and why is kept.** `cst deployments rollback <project> --history` lists every roll back: when, who, from
  which version to which, and the reason. It is also in the project's history.
- **By itself, if you ask.** `cst deployments rollback <project> --auto 30`: for the first 30 minutes after each
  deploy, when a health check finds the new version unhealthy, Captain puts the previous one back and says so in
  the alert. It does this once per deploy, never without an earlier version, and never to the version it just put
  back. `--auto 0` turns it off.

One limit on this computer: each version gets a new port, so the address changes with the version. Requests made
through Captain follow it. A program that saved the address must read it again. On a cloud the address stays.

`POST /api/projects/<id>/staging/versions/<n>/rollback {reason}`, `GET .../staging/rollbacks`,
`PUT .../staging/rollbacks/auto {minutes}`.

## Which machine, in Settings

`GET /api/serving/machines` lists, per cloud, the sizes on offer and the one chosen in place of the smallest that
fits. `PUT /api/settings/serving {host, machine}` saves a choice. An empty machine goes back to the smallest.

## Before a real site sends traffic

`cst deployments protect <project>` shows and sets three things. In the app they are in the deployed model's
Settings. Each takes effect in a few seconds on every
copy, with no restart.

- **Request size.** `--max-request-kb 256` (the default). A bigger request gets 413 and a sentence.
- **Requests a second.** `--rate 50`, or `--rate 0` for no limit (the default). More than that get 429, a
  sentence and `Retry-After: 1`. The rate is per copy: three copies take three times as many.
- **A time budget with a fallback answer.** `--budget-ms 200`. When this model takes longer, or fails, the
  request is answered with the compared model's results. The answer carries `fallback: {why, answered_by}`, and
  the request is logged under the model the shopper was given, with `fallback` on it. A fallback counts against
  the model's health like a failed request. `--no-fallback` turns the fallback off. With no compared model
  loaded, a request over the budget gets 504 and a sentence. Even with no budget, a model that fails is answered
  by the compared model unless the fallback is off.

**Changing the key with no downtime.** `cst deployments key <project> --rotate` makes a new key. The old one
keeps working for an hour (`--grace-minutes N`, 0 to end it now), so a site can switch when it suits. The copy
Captain reaches learns the new key at once, every other copy within a few seconds. `--show` prints the key. The
key also stays the same across a redeploy and a roll back. A key set in the environment is not changed here.

`GET|PUT /api/projects/<id>/staging/protect`, `POST /api/projects/<id>/staging/key/rotate {grace_minutes}`.
