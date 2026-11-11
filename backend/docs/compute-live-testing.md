# Testing a compute provider on a real account

`aws-ecs`, `aws-batch` and `local` have run real jobs (2026-11-06): submit, status, logs and cancel.
`azure-container-apps` and `azure-ml` have too (2026-11-10), with the workspace in Azure Blob: see "What the Azure
run found" below. Cloud Run, Vertex and Modal pass the same contract tests against fakes of their SDKs
(`core/tests/test_compute_providers.py`), but have not run against a real account. This page says what each one
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

## Google Vertex AI custom jobs (`gcp-vertex`)

- Install: as Cloud Run.
- Login: the same service account, plus Vertex AI User (`roles/aiplatform.user`).
- Make once: nothing beyond the image and a runtime service account with Storage Object Admin on the bucket
  (set it as `service_account`). The Vertex API must be on in the project.
- Target fields: `image`, optional `machine_type` (default `n2-standard-8`; `n2-standard-2` is enough for the demo),
  `service_account`.
- Unproven assumptions: logs are found by `resource.type="ml_job"` and the job id, and `container_spec.args`
  reaches the image's entrypoint (`tini -- cst`).

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

Not tested: serving on either kind (`cst deploy` plans it, and execution raises "planned but not executable yet"),
a service-principal login (the run used `az login`), a user-assigned identity, the starter ranker's cloud training
(no `--compute` on `cst projects starter-train` yet), and reading a dataset in place: large datasets download whole.

## Modal (`modal`)

- Install: `pip install 'captain-search[modal]'`.
- Login: a Modal token for the team's Modal workspace (`modal token new`). The provider uses Modal's own login,
  not a `cst cloud` profile.
- Make once: a Modal secret named `captain-search-runner` with credentials for the workspace bucket only (for S3, an
  access key of a user or role limited to that bucket), then
  `CST_RUNNER_IMAGE=<public image> modal deploy runner/modal_app.py`.
- Target fields: `app` (default `captain-search-runner`), `function` (default `run_cst`).
- Known limits: Modal's SDK can't tell queued from running, so both show as `running`. It has no per-call log read,
  so `cst job logs` points to the Modal dashboard.
