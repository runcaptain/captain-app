"""AWS as a host: a SageMaker endpoint. The work is serving_setup.py, unchanged. This gives it the shared shape."""
from __future__ import annotations

from ...workspace import Context
from .. import serving_setup as SS
from .base import Host


class AwsHost(Host):
    cloud, label, kind, target, login = "aws", "AWS", "aws-sagemaker", "aws-serving", "aws"
    proven = "2026-11-11"
    storage = ("The workspace's own bucket, or, for a workspace kept on this computer, a serving bucket setup makes. "
               "The endpoint's role may read the bundle and write the log under staging/ and nothing else.")
    address = ("No public address. A site calls it through AWS (sagemaker:InvokeEndpoint, signed with AWS credentials) "
               "and sends the endpoint key in the request body.")
    options = [{"name": "profile", "label": "Cloud login", "required": True}, {"name": "region", "label": "Region", "required": True},
               {"name": "name_prefix", "label": "Name prefix", "default": "captain"},
               {"name": "instance_type", "label": "Machine", "default": "ml.m5.large"},
               {"name": "source", "label": "Where the image is built", "default": "auto"},
               {"name": "bucket", "label": "Bucket for the bundle and log (for a workspace on this computer)"}]

    key = SS.KEY  # the record AWS has always kept

    def _kw(self, options: dict) -> dict:
        return {k: options[k] for k in ("name_prefix", "instance_type", "source", "bucket") if options.get(k) is not None}

    def plan(self, ctx: Context, **options) -> dict:
        return {"cloud": "aws", **SS.plan(ctx, options["profile"], options["region"], **self._kw(options))}

    def setup(self, ctx: Context, confirm: bool = False, progress=None, **options) -> dict:
        return SS.setup(ctx, options["profile"], options["region"], confirm=confirm, progress=progress, **self._kw(options))

    def status(self, ctx: Context) -> dict:
        return {"cloud": "aws", **SS.status(ctx)}

    def teardown(self, ctx: Context, progress=None, **_options) -> dict:
        return SS.teardown(ctx, progress=progress)

    def leftovers(self, ctx: Context) -> list[dict]:
        """What in the account still carries app=captain and captain:purpose=serving, asked of AWS's tag index."""
        from ... import cloud

        rec = SS.read(ctx)
        if not rec:
            return []
        sess = cloud.aws_session(rec["profile"], rec["region"])
        out = []
        tg = sess.client("resourcegroupstaggingapi")
        token = ""
        while True:
            r = tg.get_resources(TagFilters=[{"Key": k, "Values": [v]} for k, v in SS.TAGS.items()], **({"PaginationToken": token} if token else {}))
            out += [{"type": "tagged", "name": x["ResourceARN"].split(":", 5)[-1]} for x in r.get("ResourceTagMappingList", [])]
            token = r.get("PaginationToken") or ""
            if not token:
                return out
