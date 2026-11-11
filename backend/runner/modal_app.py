"""The Modal side of a `modal` compute target. Deploy once, from backend/: `modal deploy runner/modal_app.py`.

The function runs `cst <argv>` in the runner image with the workspace environment Captain passes, and returns
cst's exit code. Credentials for the workspace bucket come from a Modal secret (for S3: AWS_ACCESS_KEY_ID and
AWS_SECRET_ACCESS_KEY, plus AWS_SESSION_TOKEN for temporary ones, of a role that can read and write only that
bucket, and AWS_DEFAULT_REGION).

Set these when you deploy, to change the defaults:
  CST_MODAL_APP      the app's name (default captain-search-runner). The target's `app` field must match.
  CST_MODAL_SECRET   the secret's name (default captain-search-runner)
  CST_RUNNER_IMAGE   a registry image Modal can pull. Unset, Modal builds runner/Dockerfile from this checkout.
  CST_MODAL_TAGS     extra app tags, as key=value,key=value
"""
import os
import pathlib
import signal
import subprocess

import modal

HERE = pathlib.Path(__file__).resolve().parent
APP = os.environ.get("CST_MODAL_APP", "captain-search-runner")
SECRET = os.environ.get("CST_MODAL_SECRET", "captain-search-runner")
IMAGE = os.environ.get("CST_RUNNER_IMAGE")
TAGS = {"captain-purpose": "cst-compute",
        **dict(p.split("=", 1) for p in os.environ.get("CST_MODAL_TAGS", "").split(",") if "=" in p)}
# How long a cancelled cst gets to stop on SIGTERM before it is killed.
STOP_GRACE_SECONDS = 20

app = modal.App(APP, tags=TAGS)
if not modal.is_local():
    image = None  # inside the container the image is already built, and the Dockerfile is not there to read
elif IMAGE:
    image = modal.Image.from_registry(IMAGE).entrypoint([])
else:
    image = modal.Image.from_dockerfile(HERE / "Dockerfile", context_dir=HERE.parent).entrypoint([])


def _run(argv: list[str], workspace: dict) -> int:
    """Run cst and return its exit code. A cancel reaches this function as an exception (Modal's InputCancellation),
    not as a signal to cst, so pass it on as SIGTERM: cst then stops the way it does on the other clouds, and
    writes nothing more."""
    p = subprocess.Popen(["cst", *argv], env={**os.environ, **workspace})
    try:
        return p.wait()
    except BaseException:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        raise


@app.function(image=image, timeout=6 * 3600, secrets=[modal.Secret.from_name(SECRET)])
def run_cst(workspace: dict, argv: list[str]) -> int:
    return _run(argv, workspace)


@app.function(image=image, timeout=6 * 3600, secrets=[modal.Secret.from_name(SECRET)])
def run_backtest(workspace: dict, backtest: str) -> int:
    """Older targets call this. New ones call run_cst."""
    return _run(["-w", workspace["CST_WORKSPACE"], "backtest", "run", backtest], workspace)
