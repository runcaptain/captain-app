"""The Modal side of a staged model: one web server that runs the staging runtime (runtime.py).

Captain deploys this for you when you stage on a Modal target (compute_providers/modal_serving.py). It is the same
runtime every other host runs, over the same serving bundle, so what was tried on this computer is what Modal
serves. Modal stops every copy when nobody calls and starts one on the next request.

  image    Modal builds it from the small build folder Captain makes (the installed captain_search package and a
           Dockerfile). No registry, no Docker on this computer.
  secret   one Modal secret per deployment, deleted with it. It holds what the runtime is told: which bundle,
           where its storage is, and the SHA-256 of the endpoint key. Never the key itself.
  volume   the serving bundle, the control document and the request log (storage/modal_volume.py).

Read at deploy time, on the computer that deploys:
  CST_MODAL_SERVE_APP     the app's name          CST_MODAL_SERVE_SECRET   the secret's name
  CST_MODAL_SERVE_VOLUME  the volume's name       CST_MODAL_SERVE_BUILD    the build folder
  CST_MODAL_SERVE_CPU, _MEMORY_MB, _MIN, _MAX, _IDLE_S   the size of a copy and how many, and how long an idle one stays
  CST_MODAL_SERVE_TAGS    app tags, as key=value,key=value
"""
import os
import pathlib
import threading
import time

import modal

MOUNT = "/captain-serving"
PORT = 8080
APP = os.environ.get("CST_MODAL_SERVE_APP", "captain-staging")
SECRET = os.environ.get("CST_MODAL_SERVE_SECRET", APP)
VOLUME = os.environ.get("CST_MODAL_SERVE_VOLUME", "captain-serving")
BUILD = os.environ.get("CST_MODAL_SERVE_BUILD")
TAGS = {"app": "captain", "captain-purpose": "serving",
        **dict(p.split("=", 1) for p in os.environ.get("CST_MODAL_SERVE_TAGS", "").split(",") if "=" in p)}


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# include_source=False: the code comes from the image, so a copy starts without a mount of this computer's files.
app = modal.App(APP, tags=TAGS, include_source=False)
if modal.is_local() and BUILD:
    image = modal.Image.from_dockerfile(pathlib.Path(BUILD) / "Dockerfile", context_dir=BUILD).entrypoint([])
else:
    image = None  # inside the container the image is already built


@app.cls(image=image, secrets=[modal.Secret.from_name(SECRET)], volumes={MOUNT: modal.Volume.from_name(VOLUME)},
         cpu=_num("CST_MODAL_SERVE_CPU", 1.0), memory=int(_num("CST_MODAL_SERVE_MEMORY_MB", 2048)),
         min_containers=int(_num("CST_MODAL_SERVE_MIN", 0)), max_containers=int(_num("CST_MODAL_SERVE_MAX", 1)),
         scaledown_window=int(_num("CST_MODAL_SERVE_IDLE_S", 300)), timeout=300, startup_timeout=900)
@modal.concurrent(max_inputs=32)
class Endpoint:
    @modal.enter()
    def load(self):
        from captain_search.staging import runtime as RT
        from captain_search.workspace import open_context

        os.environ.setdefault("CST_MODAL_VOLUME_MOUNT", MOUNT)
        t0 = time.perf_counter()
        ctx = open_context(os.environ["CST_WORKSPACE"])
        self.rt = RT.Runtime(ctx, os.environ["CST_STAGING_BUNDLE"], key_sha256=os.environ.get("CST_STAGING_KEY_SHA256") or None,
                             flush_s=float(os.environ.get("CST_STAGING_FLUSH_S") or 15),
                             publish_minutes=float(os.environ.get("CST_STAGING_PUBLISH_MINUTES") or 5))
        self.api = RT.make_app(self.rt)
        self._done = threading.Event()

        def loop() -> None:
            while not self._done.wait(1.0):
                self.rt.tick()

        threading.Thread(target=loop, daemon=True, name="staging-tick").start()
        print(f"staging: loaded {', '.join(f'{n}={a.pipeline}' for n, a in self.rt.arms.items())} "
              f"in {time.perf_counter() - t0:.1f} s", flush=True)

    # A plain web server on a port, as on every other host: Modal passes HTTP straight through to it. Measured
    # 2026-11-11: it answers as fast as an ASGI app handed to Modal (about 170 ms for an empty request from a
    # laptop 75 ms away, nearly all of it the network and Modal's front door).
    @modal.web_server(port=PORT, startup_timeout=900)
    def web(self):
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(self.api, host="0.0.0.0", port=PORT, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True, name="staging-http").start()

    @modal.exit()
    def stop(self):
        # Modal is taking this copy away (idle, or a new deploy): write what is still buffered.
        self._done.set()
        if getattr(self, "server", None) is not None:
            self.server.should_exit = True
        try:
            self.rt.flush()
        except Exception as e:  # noqa: BLE001
            print(f"staging: could not write the log while stopping: {e}", flush=True)
