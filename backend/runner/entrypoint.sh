#!/bin/sh
# The runner image's entrypoint. Everything is `cst <args>`, except SageMaker's serving contract: SageMaker starts a
# serving container with the single argument `serve` and expects /ping and /invocations on port 8080. With
# CST_PIPELINE set (the serving plans set it), `serve` means "serve that pipeline", not the desktop app's daemon.
set -e
# With CST_STAGING_BUNDLE set (a staging deploy sets it), `serve` means "run the staging runtime over that bundle".
if [ "$#" -eq 1 ] && [ "$1" = "serve" ] && [ -n "$CST_STAGING_BUNDLE" ]; then
  exec cst ${CST_WORKSPACE:+-w "$CST_WORKSPACE"} projects staging-serve "$CST_STAGING_BUNDLE" --host 0.0.0.0 --port 8080
fi
if [ "$#" -eq 1 ] && [ "$1" = "serve" ] && [ -n "$CST_PIPELINE" ]; then
  exec cst ${CST_WORKSPACE:+-w "$CST_WORKSPACE"} pipeline serve "$CST_PIPELINE" --host 0.0.0.0 --port 8080
fi
exec cst "$@"
