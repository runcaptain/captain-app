#!/bin/sh
# Build the runner image for linux/amd64 (what Fargate, Batch, Cloud Run, Vertex and Azure run).
#   runner/build.sh                       build locally as captain-search-runner:<version>
#   runner/build.sh --push <image:tag>    build and push; an ECR image logs in to ECR first
set -eu
cd "$(dirname "$0")/.."
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' core/captain_search/__init__.py)
image="captain-search-runner:$version"
push=""
if [ "${1:-}" = "--push" ]; then
  push=1
  image="${2:?usage: runner/build.sh --push <image:tag>}"
fi
case "$image" in
  *.dkr.ecr.*.amazonaws.com/*)
    if [ -n "$push" ]; then
      registry="${image%%/*}"
      region=$(echo "$registry" | sed -n 's/.*\.dkr\.ecr\.\([^.]*\)\.amazonaws\.com/\1/p')
      aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
    fi ;;
esac
docker buildx build --platform linux/amd64 -f runner/Dockerfile -t "$image" --load .
# docker push, not buildx --push: it retries each layer, where buildx gives up after 60 seconds on a slow uplink
if [ -n "$push" ]; then docker push "$image"; fi
echo "$image"
