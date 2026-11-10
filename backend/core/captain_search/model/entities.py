"""The Captain Search Tools data model.

Every entity carries id, org, workspace, created_by and created_at. Every *Version is
immutable once written: it lives at <kind>/<name>/v<N>/MANIFEST.yaml next to a SHA256SUMS.
See docs/data-model.md.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

SCHEMA_VERSION = 1


def new_id() -> str:
    return str(ULID())


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


class Evidence(str, Enum):
    """How a number was produced. Ordered from weakest to strongest."""

    SIMULATED = "SIMULATED"
    LLM_GRADED = "LLM_GRADED"
    LOGGED_CLICKS = "LOGGED_CLICKS"  # grades replayed from real search logs, offline
    HUMAN_GRADED = "HUMAN_GRADED"
    REAL_TRAFFIC = "REAL_TRAFFIC"

    @property
    def rank(self) -> int:
        return list(Evidence).index(self)


class Tier(str, Enum):
    """demo deploys have no gates (SOP 2026-11-02). live deploys must pass gates."""

    demo = "demo"
    live = "live"


class ReproStatus(str, Enum):
    exact_verified = "exact-verified"
    equivalent_verified = "equivalent-verified"
    not_tested = "not-tested"


class DatasetKind(str, Enum):
    catalog = "catalog"
    events = "events"
    queries = "queries"
    judgments = "judgments"
    logs = "logs"
    training = "training"  # (query, product) rows with features and a label, built by `cst data training-set`
    generic = "generic"


class Base(BaseModel):
    # Ignore unknown fields: a teammate on a newer cst may write fields this version does not know.
    model_config = ConfigDict(extra="ignore", use_enum_values=False)

    schema_version: int = SCHEMA_VERSION
    id: str = Field(default_factory=new_id)
    org: str
    workspace: str
    created_by: str
    created_at: dt.datetime = Field(default_factory=utcnow)


# ---- workspace and connections ------------------------------------------------------


class Workspace(Base):
    kind: Literal["workspace"] = "workspace"
    name: str
    storage_uri: str
    # Access control in v1 is the bucket's IAM. This list is informational until a hosted
    # metadata service enforces it.
    members: list[str] = Field(default_factory=list)


class Network(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["direct", "ssh-tunnel", "remote-runner"] = "direct"
    ssh_host: str | None = None  # user@bastion
    ssh_port: int = 22
    runner_url: str | None = None


class Connection(Base):
    """A database the team mines. Holds no secrets: `{secret:NAME}` in the URL is resolved at
    run time from env, then keychain, then the configured secrets manager."""

    kind: Literal["connection"] = "connection"
    name: str
    engine: str  # postgres, mysql, snowflake, bigquery, redshift, athena, trino, duckdb, http
    url: str  # SQLAlchemy URL template, may contain {secret:NAME}
    network: Network = Field(default_factory=Network)
    writable: bool = False  # read-only unless explicitly marked
    row_limit_local: int = 1_000_000


# ---- versioned artifacts -------------------------------------------------------------


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str  # path inside the version, e.g. data/part-0.parquet
    sha256: str
    bytes: int


class Provenance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: Literal["sql", "file", "notebook", "generated", "import"]
    connection: str | None = None
    sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    file: str | None = None
    notebook: str | None = None
    cell: str | None = None
    ran_at: dt.datetime = Field(default_factory=utcnow)


class VersionBase(Base):
    name: str
    version: int
    parent: int | None = None
    description: str = ""
    files: list[FileEntry] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)


class ExternalFile(BaseModel):
    """A file the dataset points at but does not copy (company rule: large data stays in the cloud)."""

    model_config = ConfigDict(extra="ignore")

    uri: str  # s3://..., gs://..., az://..., modal://<volume>/<path>
    bytes: int | None = None
    etag: str | None = None
    sha256: str | None = None
    mutable: bool = False  # True for Modal volumes and other stores without object versioning


class DatasetVersion(VersionBase):
    kind: Literal["dataset"] = "dataset"
    external: list[ExternalFile] = Field(default_factory=list)
    dataset_kind: DatasetKind = DatasetKind.generic
    schema_fields: list[dict[str, str]] = Field(default_factory=list)  # [{name, type}]
    row_count: int = 0
    truncated: bool = False
    provenance: Provenance
    # queries datasets only
    splits: dict[str, list[str]] | None = None
    holdout_seal: str | None = None
    # judgments datasets only
    judgment_source: Literal["human", "llm", "heuristic", "click"] | None = None
    judge: dict[str, str] | None = None  # {model, prompt_version}
    calibration: dict[str, Any] | None = None  # {kappa, vs, ndcg_bias}


class IndexFingerprint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    doc_count: int
    sample_hash: str
    taken_at: dt.datetime = Field(default_factory=utcnow)


class PipelineVersion(VersionBase):
    kind: Literal["pipeline"] = "pipeline"
    engine: str  # elastic, algolia, typesense, captain, custom
    spec: dict[str, Any]  # stages: retrieve -> rerank -> rules -> personalize
    catalog: str | None = None  # dataset ref name@vN
    index_config: dict[str, Any] = Field(default_factory=dict)
    index_fingerprint: IndexFingerprint | None = None
    registry_ref: str | None = None  # s3://<registry>/<arch>/<brand>/<ver>
    dvc_alias: str | None = None
    training_commit: str | None = None
    repro_status: ReproStatus = ReproStatus.not_tested
    known_issues: list[str] = Field(default_factory=list)
    # What training saw and reported (`cst docs trainer-contract`). Empty on versions written before the contract.
    train_inputs: list[dict[str, Any]] = Field(default_factory=list)  # [{ref, kind, role, uri, label_source, as_of}]
    train_metrics: dict[str, Any] = Field(default_factory=dict)
    train_params: dict[str, Any] = Field(default_factory=dict)
    parents: list[str] = Field(default_factory=list)  # full refs this version came from, name@vN or an outside id
    source: dict[str, Any] | None = None  # where it came from: {kind: retrain|mlflow|registry, ...}


class ShopperPopulation(VersionBase):
    kind: Literal["population"] = "population"
    simulator: str
    simulator_version: str
    size: int
    seed: int
    personas: dict[str, Any] = Field(default_factory=dict)
    traffic: dict[str, Any] = Field(default_factory=lambda: {"shape": "zipf"})
    click_model_judgments: str | None = None  # dataset ref the click model reads, if any
    from_logs: str | None = None  # events dataset the traffic mix and click-by-rank curve came from


class Preregistration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hypothesis: str
    primary_metric: str = "ndcg@10"
    mde: float = 0.003
    written_at: dt.datetime = Field(default_factory=utcnow)


class Guardrail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metric: str  # an offline metric, e.g. ndcg@10
    min: float | None = None  # the arm must score at least this
    max_drop: float | None = None  # the arm may fall at most this far below the baseline arm


class Surface(BaseModel):
    """Another place the same pipeline serves (search, related pins, notifications), scored with its own
    queries and judgments, with guardrails that must all hold before a live promote."""

    model_config = ConfigDict(extra="ignore")

    name: str
    queries: str
    judgments: str
    guardrails: list[Guardrail] = Field(default_factory=list)


class Backtest(VersionBase):
    kind: Literal["backtest"] = "backtest"
    mode: Literal["simulated", "historic", "imported", "rankings"] = "simulated"
    logs: str | None = None  # historic mode: the events dataset replayed
    compute: str | None = None  # compute target name; None = local
    arms: list[str]  # pipeline refs
    queries: str  # dataset ref, with :split
    judgments: str | None = None
    population: str | None = None
    metrics: list[str] = Field(
        default_factory=lambda: ["ndcg@10", "p@10", "r@50", "mrr", "ctr", "conversion", "rpv", "p99_ms"]
    )
    preregistration: Preregistration
    surfaces: list[Surface] = Field(default_factory=list)


class ArmResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    arm: str
    offline: dict[str, float] = Field(default_factory=dict)  # from judgments only
    simulated: dict[str, float] = Field(default_factory=dict)  # from shopper logs only
    judged_at_10: float | None = None
    evidence: Evidence  # of the offline numbers: comes from the judgment source. Gates read this one.
    simulated_evidence: Evidence = Evidence.SIMULATED
    logs: list[FileEntry] = Field(default_factory=list)  # shopper logs and per-query rankings, stored
    cost: dict[str, Any] = Field(default_factory=dict)  # train_usd, serve_usd_per_1k, p50_ms, p99_ms, latency_source


class BacktestRun(VersionBase):
    kind: Literal["backtest-run"] = "backtest-run"
    backtest: str
    mode: Literal["simulated", "historic", "imported", "rankings"] = "simulated"
    source: str | None = None  # imported runs: where the original readout lives
    compute: str = "local"
    arms: list[ArmResult]
    comparisons: list[dict[str, Any]] = Field(default_factory=list)  # paired CI, p
    independent: bool = True  # False when the click model reads the scoring judgments
    complete: bool = True  # False when any arm has judged@10 under 0.9
    cost_usd: float = 0.0
    wall_s: float | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)  # per query class and traffic band
    surfaces: list[dict[str, Any]] = Field(default_factory=list)  # per extra surface: metrics, comparisons, guardrails
    manifest: dict[str, Any] = Field(default_factory=dict)  # the code that produced it: cst, python, project commit, extensions


class Environment(Base):
    kind: Literal["environment"] = "environment"
    name: str
    tier: Tier = Tier.demo
    compute: str | None = None  # compute target that serves this environment
    target: dict[str, Any]  # {type: captain|elastic|algolia|webhook, ...}
    gates: dict[str, Any] = Field(default_factory=dict)


class Deployment(Base):
    kind: Literal["deployment"] = "deployment"
    environment: str
    pipeline: str  # name@vN
    action: Literal["promote", "rollback", "handoff"]
    handoff: str | None = None  # live tier: the candidate bundle id. Captain never deploys to live itself
    gate_results: dict[str, Any] = Field(default_factory=dict)
    override_reason: str | None = None
    rollback_to: str | None = None
    backfilled: bool = False  # recorded after the fact from a manifest, live.json or the brain
    occurred_at: dt.datetime | None = None
    source: str | None = None


class NotebookRevision(VersionBase):
    kind: Literal["notebook"] = "notebook"


class Skill(VersionBase):
    kind: Literal["skill"] = "skill"


class RegistrySnapshot(VersionBase):
    """A read-only copy of a model registry's manifests (s3://<bucket>) at one time.
    Weights stay in the registry; only MANIFEST.yaml and the file listing are copied."""

    kind: Literal["registry"] = "registry"
    uri: str
    index_md: str = ""
    models: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    command: list[str]  # a cst command path, e.g. ["queries", "gen"]
    args: dict[str, Any] = Field(default_factory=dict)  # option name -> value; "${steps.<id>.<key>}" reads an earlier output
    needs: list[str] = Field(default_factory=list)
    when: str | None = None  # skip this step (and steps that need it) when this resolves to "", false, 0 or none
    position: dict[str, float] | None = None  # where the step sits on the canvas


class Workflow(VersionBase):
    """A saved chain of cst commands, edited on the canvas or in YAML, run by hand or on a schedule."""

    kind: Literal["workflow"] = "workflow"
    steps: list[WorkflowStep] = Field(default_factory=list)
    schedule: str | None = None  # cron, five fields, UTC; runs while a daemon is up
    params: dict[str, Any] = Field(default_factory=dict)  # "${params.<name>}" in step args


class WorkflowRun(VersionBase):
    kind: Literal["workflow-run"] = "workflow-run"
    workflow: str
    status: Literal["done", "failed"] = "done"
    steps: list[dict[str, Any]] = Field(default_factory=list)  # {id, status, exit_code, output, log, ms}
    trigger: str = "manual"


class Link(Base):
    kind: Literal["link"] = "link"
    src: str
    dst: str
    relation: Literal["produced_by", "evaluated_on", "deployed_from", "derived_from"]


class LabelDef(VersionBase):
    """A training or evaluation label defined as code: a DuckDB aggregate over an events dataset, computed
    per group (a search, a session or a user) where the data lives. Versioned, so a model records exactly
    which label definition it was trained on."""

    kind: Literal["label"] = "label"
    sql: str  # aggregate expression, e.g. max(case when clicked and dwell_s >= 5 then 1 else 0 end)
    grain: Literal["search", "session", "user", "impression"] = "search"
    where: str | None = None  # optional row filter
    dtype: Literal["binary", "number"] = "binary"


class ReplayRun(VersionBase):
    """Off-policy replay of a gating or ranking policy over logs with a randomized holdout."""

    kind: Literal["replay"] = "replay"
    logs: str
    score_col: str
    action_col: str
    propensity_col: str | None = None
    outcomes: list[str]
    direct: list[str] = Field(default_factory=list)  # outcomes the policy controls directly; the rest are directional
    curve: list[dict[str, Any]] = Field(default_factory=list)
    evidence: Evidence = Evidence.LOGGED_CLICKS
    note: str = ""


VERSION_KINDS: dict[str, type[VersionBase]] = {
    "dataset": DatasetVersion,
    "pipeline": PipelineVersion,
    "population": ShopperPopulation,
    "backtest": Backtest,
    "backtest-run": BacktestRun,
    "notebook": NotebookRevision,
    "skill": Skill,
    "registry": RegistrySnapshot,
    "workflow": Workflow,
    "workflow-run": WorkflowRun,
    "label": LabelDef,
    "replay": ReplayRun,
}

# bucket folder for each kind
KIND_DIRS = {
    "dataset": "datasets",
    "pipeline": "pipelines",
    "population": "populations",
    "backtest": "backtests",
    "backtest-run": "backtest-runs",
    "notebook": "notebooks",
    "skill": "skills",
    "registry": "registries",
    "workflow": "workflows",
    "workflow-run": "workflow-runs",
    "label": "labels",
    "replay": "replays",
}
