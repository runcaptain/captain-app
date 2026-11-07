"""Finding and making a model registry on AWS, so nobody has to type a bucket address from memory.

- discover(): every S3 bucket the login can see, each marked as a registry (models in the
  <architecture>/<brand>/<version>/MANIFEST.yaml layout, at the root or under a folder), a registry Captain made but
  still empty, an empty bucket, another kind of bucket, or one this login can't read. Registries come first.
- template(): the infrastructure for a new registry bucket, as a CloudFormation template or as Terraform, for a team
  that applies infrastructure through its own pipeline.
- create(): makes the registry now through a CloudFormation stack (so it shows in the AWS console, with its own
  history, and the team can take it over in their own infrastructure code), writes the registry's INDEX.md, and
  attaches it.

The bucket is private, encrypted, versioned (an overwritten or deleted model can be recovered), refuses plain HTTP,
and outlives its stack: deleting the stack keeps the bucket and its models.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .workspace import Context, whoami

MARKER = ".captain/registry.yaml"
MAX_BUCKETS = 300
_NAME = re.compile(r"^(?!xn--)(?!.*-s3alias$)[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_MANIFEST = re.compile(r"^(?:(.*)/)?[^/]+/[^/]+/[^/]+/MANIFEST\.yaml$")

INDEX_MD = """# Model registry

Every trained model lives at `<architecture>/<brand>/<version>/`, so the path shows which architecture a brand is on.

Each version folder holds `MANIFEST.yaml` (status, parent, results, known issues), `REPRODUCE.md`, `reproduce.sh`,
`weights/`, `code/`, `env/`, `inputs/`, `results/` and `SHA256SUMS`.

Versions are write-once: a retrain is a new version, and old ones are never overwritten or deleted. The bucket keeps
every object version, so a mistake can be undone.

Made by Captain for {who} on {when}.
"""


def _session(profile):
    from .cloud import aws_session

    return aws_session(profile)


def _region_of(s3, bucket: str) -> str:
    loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)


def _code(e) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) or type(e).__name__


def _probe(sess, bucket: str, created) -> dict:
    """What one bucket holds, in at most a handful of requests."""
    out = {"bucket": bucket, "region": None, "created": created.isoformat() if created else None,
           "kind": "other", "uri": f"s3://{bucket}", "detail": ""}
    try:
        region = _region_of(sess.client("s3"), bucket)
        out["region"] = region
        s3 = sess.client("s3", region_name=region)
        for key in (MARKER, "INDEX.md"):
            try:
                s3.head_object(Bucket=bucket, Key=key)
                out["kind"], out["detail"] = "registry", "has a registry index"
                if key == MARKER:
                    out["detail"] = "made by Captain"
                break
            except Exception as e:  # noqa: BLE001
                if _code(e) not in ("404", "NoSuchKey", "NotFound"):
                    raise
        top = s3.list_objects_v2(Bucket=bucket, Delimiter="/", MaxKeys=100)
        prefixes = [p["Prefix"] for p in top.get("CommonPrefixes", [])]
        if not prefixes and not top.get("Contents"):
            if out["kind"] != "registry":
                out["kind"], out["detail"] = "empty", "empty"
            return out
        roots: dict[str, int] = {}
        for pfx in prefixes[:6]:
            page = s3.list_objects_v2(Bucket=bucket, Prefix=pfx, MaxKeys=1000)
            for o in page.get("Contents", []):
                m = _MANIFEST.match(o["Key"])
                if m:
                    root = m.group(1) or ""
                    roots[root] = roots.get(root, 0) + 1
        seen: dict[str, bool] = {}

        def workspace_at(root: str) -> bool:
            # A Captain workspace keeps datasets/<name>/<version>/MANIFEST.yaml (names may hold folders), the same
            # shape as a registry: a candidate inside a workspace, at any depth, isn't one.
            parts = root.split("/") if root else []
            for i in range(len(parts) + 1):
                anc = "/".join(parts[:i])
                if anc not in seen:
                    try:
                        s3.head_object(Bucket=bucket, Key=f"{anc}/workspace.yaml" if anc else "workspace.yaml")
                        seen[anc] = True
                    except Exception:  # noqa: BLE001
                        seen[anc] = False
                if seen[anc]:
                    return True
            return False

        workspaces = [r for r in roots if workspace_at(r)]
        for r in workspaces:
            roots.pop(r)
        if roots:
            root = max(roots, key=lambda r: (roots[r], -len(r)))
            out["kind"] = "registry"
            out["uri"] = f"s3://{bucket}/{root}".rstrip("/")
            out["detail"] = "models in architecture / brand / version folders" + (f", under {root}/" if root else "")
            out["architectures"] = len(prefixes) if not root else None
        elif workspaces and out["kind"] != "registry":
            out["kind"], out["detail"] = "workspace", "holds a Captain workspace, not models"
        elif out["kind"] != "registry":
            out["detail"] = f"{len(prefixes)} folder{'s' if len(prefixes) != 1 else ''}, no model manifests"
    except Exception as e:  # noqa: BLE001
        code = _code(e)
        out["kind"] = "no-access"
        out["detail"] = "this login can't read it" if "AccessDenied" in code or code in ("403", "AllAccessDisabled") \
            else f"couldn't read it ({code})"
    return out


def discover(ctx: Context | None, profile: str | None = None) -> dict:
    """{account, buckets: [{bucket, region, kind, uri, detail, attached}], truncated}. Reads only."""
    sess = _session(profile)
    try:
        listed = sess.client("s3").list_buckets().get("Buckets", [])
    except Exception as e:  # noqa: BLE001
        code = _code(e)
        if "AccessDenied" in code:
            raise PermissionError("This login isn't allowed to list S3 buckets. Type the registry's address instead, "
                                  "or pick a login that can (s3:ListAllMyBuckets).") from None
        if "Credential" in code or "Token" in code or "Expired" in code or "NoCredentials" in type(e).__name__:
            raise PermissionError("The AWS login didn't work. Check it in Settings, Cloud and compute.") from None
        raise
    try:
        account = sess.client("sts").get_caller_identity().get("Account")
    except Exception:  # noqa: BLE001
        account = None
    listed = listed[:MAX_BUCKETS]
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(lambda b: _probe(sess, b["Name"], b.get("CreationDate")), listed))
    attached = set()
    if ctx is not None:
        from .attach import _read

        attached = {r["uri"].rstrip("/") for r in _read(ctx)[0]["registries"]}
    order = {"registry": 0, "empty": 1, "other": 2, "workspace": 3, "no-access": 4}
    for r in rows:
        r["attached"] = r["uri"].rstrip("/") in attached
    rows.sort(key=lambda r: (order.get(r["kind"], 9), r["bucket"]))
    return {"account": account, "buckets": rows, "truncated": len(listed) >= MAX_BUCKETS,
            "registries": sum(1 for r in rows if r["kind"] == "registry")}


# ---- making one -----------------------------------------------------------------------------------------------------


def check_name(bucket: str) -> str:
    b = (bucket or "").strip().lower()
    if not _NAME.match(b) or ".." in b:
        raise ValueError("a bucket name is 3 to 63 lowercase letters, digits and hyphens, starting and ending with a "
                         "letter or digit")
    return b


def stack_name(bucket: str) -> str:
    return f"captain-model-registry-{bucket}"[:128]


CLOUDFORMATION = """AWSTemplateFormatVersion: "2010-09-09"
Description: Captain model registry. Trained models at <architecture>/<brand>/<version>/, write-once.
Parameters:
  BucketName:
    Type: String
    Description: The registry bucket's name.
Resources:
  Registry:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain          # deleting the stack keeps the bucket and its models
    UpdateReplacePolicy: Retain
    Properties:
      BucketName: !Ref BucketName
      VersioningConfiguration:
        Status: Enabled             # an overwritten or deleted model can be recovered
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: AES256
            BucketKeyEnabled: true
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      OwnershipControls:
        Rules:
          - ObjectOwnership: BucketOwnerEnforced
      LifecycleConfiguration:
        Rules:
          - Id: abort-incomplete-uploads
            Status: Enabled
            AbortIncompleteMultipartUpload:
              DaysAfterInitiation: 7
      Tags:
        - Key: managed-by
          Value: captain
        - Key: purpose
          Value: model-registry
  RegistryPolicy:
    Type: AWS::S3::BucketPolicy
    Properties:
      Bucket: !Ref Registry
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: DenyPlainHttp
            Effect: Deny
            Principal: "*"
            Action: "s3:*"
            Resource:
              - !GetAtt Registry.Arn
              - !Sub "${Registry.Arn}/*"
            Condition:
              Bool:
                aws:SecureTransport: "false"
Outputs:
  BucketName:
    Value: !Ref Registry
  RegistryUri:
    Value: !Sub "s3://${Registry}"
"""

TERRAFORM = """# Captain model registry. Trained models at <architecture>/<brand>/<version>/, write-once.
# terraform init && terraform apply -var bucket_name={bucket} -var region={region}

terraform {{
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = ">= 5.0" }}
  }}
}}

variable "bucket_name" {{
  type    = string
  default = "{bucket}"
}}

variable "region" {{
  type    = string
  default = "{region}"
}}

provider "aws" {{
  region = var.region
}}

resource "aws_s3_bucket" "registry" {{
  bucket = var.bucket_name
  tags   = {{ managed-by = "captain", purpose = "model-registry" }}

  lifecycle {{
    prevent_destroy = true # the bucket holds every trained model
  }}
}}

resource "aws_s3_bucket_versioning" "registry" {{
  bucket = aws_s3_bucket.registry.id
  versioning_configuration {{
    status = "Enabled" # an overwritten or deleted model can be recovered
  }}
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "registry" {{
  bucket = aws_s3_bucket.registry.id
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
    bucket_key_enabled = true
  }}
}}

resource "aws_s3_bucket_public_access_block" "registry" {{
  bucket                  = aws_s3_bucket.registry.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

resource "aws_s3_bucket_ownership_controls" "registry" {{
  bucket = aws_s3_bucket.registry.id
  rule {{
    object_ownership = "BucketOwnerEnforced"
  }}
}}

resource "aws_s3_bucket_lifecycle_configuration" "registry" {{
  bucket = aws_s3_bucket.registry.id
  rule {{
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {{}}
    abort_incomplete_multipart_upload {{
      days_after_initiation = 7
    }}
  }}
}}

resource "aws_s3_bucket_policy" "registry" {{
  bucket = aws_s3_bucket.registry.id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Sid       = "DenyPlainHttp"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.registry.arn, "${{aws_s3_bucket.registry.arn}}/*"]
      Condition = {{ Bool = {{ "aws:SecureTransport" = "false" }} }}
    }}]
  }})
  depends_on = [aws_s3_bucket_public_access_block.registry]
}}

output "registry_uri" {{
  value = "s3://${{aws_s3_bucket.registry.id}}"
}}
"""


def template(bucket: str, region: str = "us-west-1", fmt: str = "terraform") -> dict:
    """{format, filename, text}: the registry bucket as infrastructure code, to apply through a team's own pipeline."""
    bucket = check_name(bucket)
    if fmt == "cloudformation":
        return {"format": "cloudformation", "filename": "captain-model-registry.yaml", "text": CLOUDFORMATION,
                "how": f"aws cloudformation deploy --template-file captain-model-registry.yaml --stack-name "
                       f"{stack_name(bucket)} --parameter-overrides BucketName={bucket} --region {region}"}
    if fmt != "terraform":
        raise ValueError("pick terraform or cloudformation")
    return {"format": "terraform", "filename": "captain-model-registry.tf",
            "text": TERRAFORM.format(bucket=bucket, region=region), "how": "terraform init && terraform apply"}


def _plain_cfn(e) -> str:
    code, msg = _code(e), str(getattr(e, "response", {}).get("Error", {}).get("Message", "")) or str(e)
    if "AccessDenied" in code or "not authorized" in msg:
        return ("This login isn't allowed to create the registry. It needs cloudformation:CreateStack and "
                "s3:CreateBucket, s3:PutBucket* and s3:PutEncryptionConfiguration. Or download the Terraform and "
                "apply it yourself.")
    if code == "AlreadyExistsException":
        return "A Captain registry stack for that bucket already exists. Pick it from the list instead."
    return msg.splitlines()[0][:300]


def create(ctx: Context, bucket: str, region: str = "us-west-1", profile: str | None = None, label: str | None = None,
           progress=lambda f: None) -> dict:
    """Make a registry bucket through a CloudFormation stack, write its INDEX.md, and attach it."""
    from . import attach as AT

    bucket = check_name(bucket)
    sess = _session(profile)
    s3 = sess.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket)
        raise ValueError(f"a bucket named {bucket} already exists in this account. Pick it from the list instead.")
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        code = _code(e)
        if code in ("403", "AccessDenied"):
            raise ValueError(f"the name {bucket} is taken (bucket names are shared across every AWS account). "
                             "Try another.") from None
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    progress(0.05)
    cfn = sess.client("cloudformation", region_name=region)
    name = stack_name(bucket)
    try:
        cfn.create_stack(StackName=name, TemplateBody=CLOUDFORMATION,
                         Parameters=[{"ParameterKey": "BucketName", "ParameterValue": bucket}],
                         Tags=[{"Key": "managed-by", "Value": "captain"}, {"Key": "purpose", "Value": "model-registry"}],
                         OnFailure="DELETE")
    except Exception as e:  # noqa: BLE001
        msg = _plain_cfn(e)
        raise (PermissionError if msg.startswith("This login isn't allowed") else RuntimeError)(msg) from None
    t0 = time.time()
    while True:
        try:
            status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except Exception:  # noqa: BLE001  rolled back and deleted (OnFailure=DELETE)
            status = "DELETE_COMPLETE"
        if not status.endswith("IN_PROGRESS") or time.time() - t0 > 600:
            break
        progress(min(0.8, 0.1 + (time.time() - t0) / 90))
        time.sleep(4)
    if status != "CREATE_COMPLETE":
        why = ""
        try:
            for ev in cfn.describe_stack_events(StackName=name)["StackEvents"]:
                if ev.get("ResourceStatus") == "CREATE_FAILED" and ev.get("ResourceStatusReason"):
                    why = ev["ResourceStatusReason"]
        except Exception:  # noqa: BLE001  a stack rolled back with OnFailure=DELETE may be gone already
            pass
        raise RuntimeError(f"AWS couldn't create the registry ({status.lower().replace('_', ' ')})"
                           + (f": {why.splitlines()[0][:240]}" if why else "."))
    progress(0.85)
    who, when = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    s3.put_object(Bucket=bucket, Key="INDEX.md", Body=INDEX_MD.format(who=who, when=when[:10]).encode(),
                  ContentType="text/markdown")
    s3.put_object(Bucket=bucket, Key=MARKER, ContentType="application/yaml",
                  Body=f"layout: architecture/brand/version\nmade_by: {who}\nmade_at: {when}\nstack: {name}\n"
                       f"region: {region}\n".encode())
    progress(0.9)
    att = AT.attach(ctx, f"s3://{bucket}", profile, label or bucket)
    progress(1.0)
    return {"uri": f"s3://{bucket}", "region": region, "stack": name, "profile": profile, **att}
