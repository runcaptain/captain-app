# Testing a compute provider on a real account

`aws-ecs`, `aws-batch` and `local` have run real jobs (2026-11-06): submit, status, logs and cancel. Cloud Run,
Vertex, Container Apps, Azure ML and Modal pass the same contract tests against fakes of their SDKs
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

- Install: `pip install 'captain-search[azure,compute-azure]'`.
- Login: a service principal with Contributor on one resource group, plus the subscription ID.
  `cst cloud add <login> --provider azure --auth service-principal -f tenant_id=<t> -f client_id=<c> -f subscription_id=<s> -s client_secret`
- Make once: a Container Apps environment with a Log Analytics workspace, and a manual-trigger job whose
  container is named `runner` and runs the runner image. Give the job a managed identity with Storage Blob Data
  Contributor on the workspace's storage account.
  `az containerapp job create -n <job> -g <rg> --environment <env> --trigger-type Manual --image <image> --container-name runner --replica-timeout 3600 --replica-retry-limit 0`
- Target fields: `resource_group`, `job`, `image`, `log_analytics_workspace_id` (the workspace's customer ID, for logs).
- Unproven assumptions: an execution's console lines are in `ContainerAppConsoleLogs_CL` under its name, and they
  arrive a few minutes late, so logs right after a job lag.

## Azure Machine Learning command jobs (`azure-ml`)

- Install: as Container Apps.
- Login: the same service principal, with Contributor (or AzureML Data Scientist) on the ML workspace.
- Make once: an Azure ML workspace and a compute cluster with min nodes 0, so it costs nothing while idle. Give the
  cluster a managed identity with Storage Blob Data Contributor on the workspace's storage account.
  `az ml compute create -n <cluster> -t AmlCompute --min-instances 0 --max-instances 1 --size Standard_DS2_v2 -g <rg> -w <aml-ws>`
- Target fields: `resource_group`, `workspace_name`, `compute`, `image`.
- Unproven assumptions: the job's `user_logs/std_log*.txt` holds cst's output, and a cluster scaling from zero takes
  several minutes, so expect a long `queued`.

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
