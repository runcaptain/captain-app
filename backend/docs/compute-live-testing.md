# Testing a compute provider on a real account

`aws-ecs`, `aws-batch` and `local` have run real jobs (2026-11-06): submit, status, logs and cancel. `modal` has
too (2026-11-10, see its section). Cloud Run, Vertex, Container Apps and Azure ML pass the same contract tests
against fakes of their SDKs (`core/tests/test_compute_providers.py`), but have not run against a real account. This page says what each one
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
- Modal does not serve pipelines from Captain (`cst` reports "modal cannot serve models").
