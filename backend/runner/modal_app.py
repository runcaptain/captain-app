"""The Modal side of a `modal` compute target. Deploy once: `modal deploy runner/modal_app.py`.

The function runs `cst <argv>` in the runner image with the workspace environment Captain passes, and returns
cst's exit code. Credentials for the workspace bucket come from a Modal secret named `captain-search-runner`
(for S3: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY of a role that can read and write only that bucket).
"""
import os
import subprocess

import modal

IMAGE = os.environ.get("CST_RUNNER_IMAGE", "ghcr.io/captain/captain-search-runner:0.1.0")
app = modal.App("captain-search-runner")
image = modal.Image.from_registry(IMAGE).entrypoint([])


@app.function(image=image, timeout=6 * 3600, secrets=[modal.Secret.from_name("captain-search-runner")])
def run_cst(workspace: dict, argv: list[str]) -> int:
    return subprocess.run(["cst", *argv], env={**os.environ, **workspace}).returncode


@app.function(image=image, timeout=6 * 3600, secrets=[modal.Secret.from_name("captain-search-runner")])
def run_backtest(workspace: dict, backtest: str) -> int:
    """Older targets call this. New ones call run_cst."""
    return subprocess.run(["cst", "-w", workspace["CST_WORKSPACE"], "backtest", "run", backtest],
                          env={**os.environ, **workspace}).returncode
