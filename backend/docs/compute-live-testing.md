# Testing a compute provider on a real account

`aws-ecs`, `aws-batch` and `local` have run real jobs (2026-11-06): submit, status, logs and cancel. `modal` has
too (2026-11-10, see its section), and so have `azure-container-apps` and `azure-ml` (2026-11-10), with the
workspace in Azure Blob: see "What the Azure run found" below. Cloud Run and Vertex pass the same contract tests
against fakes of their SDKs (`core/tests/test_compute_providers.py`), but have not run a job against a real account
(Google Cloud Storage workspaces have: see that section). This page says what each one
needs before it can, and how to run the test.

Use an account the team owns, never a person's own login. Secrets go in the OS keychain through `cst cloud add`,
never in a file. Tag or label everything you make so it can be found and removed later.

## The test, for every kind

The workspace must be in the same cloud's storage (`gs://` for Google, `az://` for Azure; Modal can use any),
because the job reads and writes it there.

```sh
cst compute add <target> --kind <kind> --profile <login> --region <region> --set field=value ...
cst compute test <target>                       # read-only: the login works and the resources exist
cst job run <target> -- demo seed --legacy      # a small fixture, made in the cloud, not uploaded
cst job show <job-id> --wait                    # queued, running, succeeded, with a run time
cst job logs <job-id>                           # the lines cst printed
cst backtest run demo-tuned-vs-bm25 --compute <target>
cst job run <target> -- backtest run demo-tuned-vs-bm25   # then, once it is running:
cst job cancel <job-id>                         # it should end "stopped", exit code 143, and write no new run
```

Pass when: the backtest's new run version is in the bucket and its report says it ran on `<target>`, the logs show
its output, and the cancelled job wrote nothing. Then set `live_tested` on the provider class to that date.

Every kind runs the runner image (`runner/Dockerfile`). Build it with `runner/build.sh --push <registry>/<repo>:<version>`
into a registry that cloud can pull from (Artifact Registry, Azure Container Registry, or a public one).

## Google Cloud Storage workspaces (`gs://`)

Run on a real bucket on 2026-11-10, signed in with application default credentials (`cst cloud add <login>
--provider gcp --auth adc -f project=<project-id>`). No storage code needed a fix. What ran:

- Create-only writes: a second write of an existing key is refused, and of 8 writers racing for one key exactly
  one wins. Compare-and-swap: of 8 writers racing on one generation exactly one wins, and a stale generation is
  refused, as is a generation on a key that does not exist.
- Six concurrent publishers of one pipeline got six different version numbers, and the versions verify.
  Six concurrent writers of one alias: one wins.
- `cst init --storage gs://<bucket>/<prefix>`, `cst demo seed --legacy`, `cst sync`, `cst index`, `cst data import`
  and `cst data browse` (the file is downloaded once into `$CST_HOME/blob-cache`), `cst lineage` and
  `cst lineage reindex`.
- `cst models publish --registry gs://<bucket>/registry` and `cst models pull`, with the files checked against
  SHA256SUMS. A second publish makes a new version and leaves the first untouched.

Known limits: listing is by key prefix, as on S3, so `list("a/b")` also returns `a/bc.txt`: pass a trailing slash.
There is no multipart path, so `put_new_chunks` joins the chunks in memory before one create-only upload.

## Google Cloud Run jobs (`gcp-cloud-run`)

- Install: `pip install 'captain-search[gcs,compute-gcp]'`.
- Login: a service account key in the team's project, with roles Cloud Run Developer (`roles/run.developer`),
  Logs Viewer (`roles/logging.viewer`) and Service Account User on the job's runtime account.
  `cst cloud add <login> --provider gcp --auth service-account -f project=<project-id> -s service_account_json`
- Make once: a Cloud Run job that runs the runner image, in the region you pass. Its runtime service account
  needs Storage Object Admin on the workspace bucket. Captain overrides only the args and env of each run.
  `gcloud run jobs create <job> --image <image> --region <region> --service-account <runtime-sa> --task-timeout 3600 --max-retries 0`
- Target fields: `job=<job>`, `image=<image>`.
- Unproven assumptions: the execution name comes from the run operation's metadata, and logs are found by the
  `run.googleapis.com/execution_name` label.
- Run on 2026-11-10, read-only calls only: `cst compute add` and `cst compute test` (it names a job that does
  not exist, in Cloud Run's words), and the log query, which Cloud Logging accepts. No job ran: the runner image
  could not be built that day. Signing in with application default credentials (`--auth adc`) works for these.
- Turning the Cloud Run API on grants `roles/run.serviceAgent` on the project to Google's Cloud Run service
  agent. Turning the API off again leaves that grant behind.

## Google Vertex AI custom jobs (`gcp-vertex`)

- Install: as Cloud Run.
- Login: the same service account, plus Vertex AI User (`roles/aiplatform.user`).
- Make once: nothing beyond the image and a runtime service account with Storage Object Admin on the bucket
  (set it as `service_account`). The Vertex API must be on in the project.
- Target fields: `image`, optional `machine_type` (default `n2-standard-8`; `n2-standard-2` is enough for the demo),
  `service_account`.
- Unproven assumptions: logs are found by `resource.type="ml_job"` and the job id, and `container_spec.args`
  reaches the image's entrypoint (`tini -- cst`).
- Run on 2026-11-10, read-only calls only: `cst compute test` (fixed that day: it passed `page_size` as a
  keyword the client does not take) and the log query, which Cloud Logging accepts. No job ran.

## Azure Container Apps jobs (`azure-container-apps`)

Tested 2026-11-10.

- Install: `pip install 'captain-search[azure,compute-azure]'`.
- Login: a service principal with Contributor on one resource group, plus the subscription ID.
  `cst cloud add <login> --provider azure --auth service-principal -f tenant_id=<t> -f client_id=<c> -f subscription_id=<s> -s client_secret`
  The test used `--auth default -f subscription_id=<s>` (the `az login` on the computer). That login needs
  Storage Blob Data Contributor on the storage account to open the workspace, and Log Analytics Reader to read logs.
- The subscription must have `Microsoft.App`, `Microsoft.OperationalInsights` and `Microsoft.ContainerRegistry`
  registered (`az provider register -n <name>`), once.
- Make once: a Container Apps environment with a Log Analytics workspace, and a manual-trigger job whose
  container is named `runner` and runs the runner image. Give the job a managed identity with Storage Blob Data
  Contributor on the workspace's storage account. With the image in Azure Container Registry, the same identity
  pulls it (`--registry-identity system` grants AcrPull).
  `az containerapp job create -n <job> -g <rg> --environment <env> --trigger-type Manual --image <image> --container-name runner --cpu 1 --memory 2Gi --replica-timeout 3600 --replica-retry-limit 0 --mi-system-assigned --registry-server <registry>.azurecr.io --registry-identity system`
- Size the job itself (`--cpu`, `--memory`): every run uses the job's size. 1 CPU and 2 GB runs the demo. The
  smallest size (0.5 CPU, 1 GB) is killed for memory during `demo seed --legacy`.
- A user-assigned identity works too: set `AZURE_CLIENT_ID` to its client ID in the job's environment. Captain
  keeps the job's own variables on every run.
- Target fields: `resource_group`, `job`, `image`, `log_analytics_workspace_id` (the workspace's customer ID, for logs).
- Known limits: logs come from Log Analytics (`ContainerAppConsoleLogs_CL`) and arrive a minute or two after the
  job prints them. Container Apps reports no exit code, so `exit_code` stays empty: a cancelled job ends `stopped`
  with no 143. A cancel takes about 12 seconds to return.

## Azure Machine Learning command jobs (`azure-ml`)

Tested 2026-11-10.

- Install: as Container Apps.
- Login: the same service principal, with Contributor (or AzureML Data Scientist) on the ML workspace.
- The subscription must also have `Microsoft.MachineLearningServices` and `Microsoft.KeyVault` registered.
- Make once: an Azure ML workspace and a compute cluster with min nodes 0, so it costs nothing while idle. Give the
  cluster a system-assigned managed identity with Storage Blob Data Contributor on the workspace's storage account,
  and AcrPull on the registry that holds the runner image.
  `az ml compute create -n <cluster> -t AmlCompute --min-instances 0 --max-instances 1 --size Standard_DS2_v2 --identity-type SystemAssigned -g <rg> -w <aml-ws>`
- Target fields: `resource_group`, `workspace_name`, `compute`, `image`.
- Known limits: a cluster scaling from zero took about 3 minutes before the job's first line, and a warm one about
  40 seconds. `started_at` is when the job was created, not when its container started. No exit code. Logs are the
  job's `user_logs/std_log*.txt`, downloaded whole on each read. Creating the ML workspace also creates a storage
  account, a key vault, Application Insights and a Log Analytics workspace in the resource group. The key vault
  is soft-deleted with the group: purge it (`az keyvault purge`) if the name must be free again.

## What the Azure run found (2026-11-10)

One scratch resource group, everything in it, deleted after. The workspace was `az://<account>/<container>/<prefix>`.

Passed, on both kinds: `cst compute test`, `cst job run <target> -- demo seed --legacy` into an empty workspace,
`cst backtest run demo-tuned-vs-bm25 --compute <target>` (the report says it ran on the target, and its live run
streamed 493 events in 6 batches into the bucket), `cst job logs`, a cancel that left the run count unchanged, a
cancel of a long-running process, and `cst pipeline retrain <ref> --compute <target>` with a trivial training
command (pending at once, then the job published the new version; `--wait` returns it).

Passed, on storage: `cst init --storage az://...`, a second write to a published version refused, 8 writers racing
one pointer with exactly one winner, listing, a 70 MB write, `cst data import` and read back, the lineage index,
and `cst models publish` to a prefix in the container then `cst models pull` with checksums.

Fixed on the way (each has a unit test):

1. Pointer writes crashed: `TypeError: 'BlobClient' object is not subscriptable`. The pointer was written, then the
   caller got the error.
2. `demo seed --legacy` stopped with `browsing blobs in GCS or Azure workspaces is not supported yet`. Blobs are now
   copied to a local cache and read there. The whole file comes down on first read.
3. The first job died with `ModuleNotFoundError: No module named 'azure.identity'`: the `azure` extra, which is all
   the runner image installs, lacked it.
4. A job made with 1 CPU and 2 GB ran at 0.5 CPU and 1 GB and was killed (`exit code '137'`): the start request
   replaced the job's container, size included.
5. Container Apps log lines came back out of order, and a second read with the cursor could lose lines printed in
   the same second.
6. A cancelled job had no end time, and Azure ML's status sentence read `JobStatus.QUEUED`.

Not tested: serving on either kind. `cst deploy` plans it, and execution raises "planned but not executable yet".
Staging a model on Container Apps (`cst projects stage`) is built since 2026-11-11 and tested against a stand-in
only: see "Hosts" in `cst docs staging`. Also not tested:
a service-principal login (the run used `az login`), a user-assigned identity, the starter ranker's cloud training
(no `--compute` on `cst projects starter-train` yet), and reading a dataset in place: large datasets download whole.

## Modal (`modal`)

Tested on a real Modal workspace on 2026-11-10, client 1.6.3, CPU containers, with the workspace in S3.

- Install: `pip install 'captain-search[modal]'` (Modal 1.6 or newer).
- Login: a Modal token for the team's Modal workspace (`modal token new`). The provider uses Modal's own login,
  not a `cst cloud` profile.
- Make once:
  1. A Modal secret with credentials for the workspace bucket only. For S3: `AWS_ACCESS_KEY_ID`,
     `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, and `AWS_SESSION_TOKEN` when the keys are temporary. The test
     used four-hour keys from `aws sts get-federation-token` with a policy limited to the workspace prefix.
  2. From `backend/`: `modal deploy runner/modal_app.py`. Modal builds `runner/Dockerfile` itself, so no registry
     is needed (about 90 seconds the first time). Set `CST_RUNNER_IMAGE` to use a registry image instead.
  3. To use other names or a separate Modal environment:
     `CST_MODAL_APP=<app> CST_MODAL_SECRET=<secret> CST_MODAL_TAGS=team=search modal deploy -e <environment> runner/modal_app.py`
- Target fields: `app` (default `captain-search-runner`), `function` (default `run_cst`), `environment` (blank for
  your login's default).
  `cst compute add modal --kind modal --set app=<app> --set environment=<environment>`

What ran, and how long it took:

| Step | Result |
| --- | --- |
| `cst compute test` | found the deployed function. Without `environment` it failed with "App ... not found in environment 'main'" |
| `cst job run <target> -- demo seed --legacy` | succeeded, 39 s from submit with a cold container |
| `cst backtest run demo-tuned-vs-bm25 --compute <target>` | succeeded in 21 s. The run is in the bucket and its report says "on <target>" |
| Live view of that run | 493 events in 6 batches read from the bucket: queued, running, done |
| `cst job logs` | the call's own lines, with times |
| `cst job cancel` on a retrain 15 s into a 90 s training command | "stopped" 12 s later, no new pipeline version, no new objects |
| `cst pipeline retrain <name> --compute <target>` | returned at once with no new version, then the job published v2 |
| the same with `--wait` | returned v3 |

What the first run on a real account changed:

- The default image (`ghcr.io/captain/captain-search-runner:0.1.0`) can't be pulled ("denied"), so the documented
  deploy failed. The app now builds the Dockerfile on Modal unless `CST_RUNNER_IMAGE` is set.
- The app and secret names were fixed in the file, so a second copy or a test copy could not be deployed.
- A target could not name a Modal environment, so an app outside the login's default one was not found.
- A cancelled call showed as "failed". Modal raises `RemoteError: Function call was cancelled by user or a failure.`
  for it, and the provider looked for "Cancel" in the class name.
- `cst job cancel` recorded "stopped" without asking Modal, so a job that had already returned was recorded as
  stopped. It now reports what Modal says.
- Modal cancels by raising inside the function, which does not reach cst. The function now sends cst SIGTERM and
  waits up to 20 s, so cst stops as it does on the other clouds.
- `cst job logs` pointed at the dashboard. Modal 1.6 reads one call's lines (`FunctionCall.logs`), so it now
  returns them.

Known limits:

- Modal's SDK can't tell queued from running, so both show as `running`, and a job has no start time.
- A cancelled call has no exit code (AWS gives 143), and its finish time is when Captain first saw it stopped.
- A cancel sent in the last seconds of a job can lose the race: the demo evaluation computes in about 2 s, and one
  cancel arrived after the run was written. Modal still reports that call as cancelled.
- Not run: GPUs, a workspace in `gs://` or `az://`, the older `run_backtest` function, and jobs longer than a few
  minutes (the function's limit is 6 hours).
- A Modal function target (`modal`) runs jobs only. Hosting a staged model on Modal is its own kind, `modal-endpoint`,
  made by `cst compute setup serving modal` and run on a real workspace on 2026-11-11: see "Hosts" in `cst docs staging`.
