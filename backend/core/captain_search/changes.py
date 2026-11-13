"""One change feed for the app: what changed, the moment it changes.

Every screen used to keep itself current with its own timer (104 of them, at 1 to 60 seconds), and a row could
say "Stopping" for two hours after the model was gone. This is the one channel they listen to instead.

A process-wide sequence number and a set of coarse topics. Every write bumps the sequence and records which
topics changed. A client says which topics it shows and is told the moment one of them changes, then reads again
what it already reads. The feed carries no data: only "this changed".

    projects, project:<id>          data, table:<name>          runs, run:<id>          jobs, job:<id>
    deployed, deployment:<project>  notebooks                   logins      compute     settings     team

A change to one thing bumps its own topic and its list's (project:shop and projects), so a list listens to the
plural and a page to its own.

Where bumps come from
- Every write through this process: the storage layer calls `wrote(store, key)` after each put, so no caller
  can forget (storage/backend.py). `topics_for` maps the key to its topics.
- State that is not a store write: a job's progress, a live run's events, a cloud login saved. Those call `bump`.
- Writes made outside this process (a `cst` command in a terminal, an agent, another machine): `Watcher`, while
  anyone is listening. A workspace on this computer is seen within about a second. One in a bucket within about
  two, and only for its few index objects (see `BucketWatch`).
- What only a cloud knows (a model that stopped by itself): `staging/cloud_watch.py`, while anyone listens to
  `deployed`.

The wire is `GET /api/changes` (a long poll) and `/api/changes/ws` in server.py.
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import threading
import time

KEEP = 4096          # how many changes the feed remembers: a client further behind is told to read everything again
HOLD_S = 25.0        # how long a long poll is held open with nothing to say
LINGER_S = 30.0      # how long the watchers keep going after the last listener left (a long poll comes straight back)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- which topics a write touches --------------------------------------------------------------------------------

_DATA = ("classes/", "classifications/", "connections/", "metastore/", "labels/", "populations/", "lineage", "signals/", "snippets/")
_RUNS = ("pipelines/", "backtests/", "replays/", "bench/", "grading/", "investigations/", "decisions/", "handoffs/", "deployments/",
         "environments/", "aliases/", "monitor/")
_SETTINGS = ("settings.yaml", "costs/", "policies/", "registries.yaml", "mlflow.yaml", "scopes.yaml", "scope-model.yaml", "workspace.yaml")


def topics_for(key: str) -> set[str]:
    """The topics a write to this storage key touches. Coarse on purpose: a screen reads again what it shows."""
    k = str(key or "").lstrip("/")
    parts = k.split("/")
    head = parts[0]
    if k == "projects.yaml":
        return {"projects"}
    if head == "projects" and len(parts) > 1:
        return {"projects", f"project:{parts[1]}"}
    if head == "staging" and len(parts) > 1:
        pid = parts[1]
        # What the model writes about itself (its log, its copies, its health checks) changes the deployment, not
        # the project's page.
        own = len(parts) > 3 and parts[3] in ("logs", "copies", "health", "stopped")
        return {"deployed", f"deployment:{pid}"} | (set() if own else {"projects", f"project:{pid}"})
    if head == "datasets" and len(parts) > 1:
        # datasets/<name>/... and datasets/logs/<name>/...: the table is named by everything before its version.
        name = "/".join(p for p in parts[1:-1] if not p.startswith("v") or not p[1:].isdigit()) or parts[1]
        return {"data", f"table:{name}"}
    if head in ("library.yaml", "media.yaml") or k.startswith(_DATA):
        return {"data"}
    if head == "backtest-runs" and len(parts) > 1:
        return {"runs", f"run:{parts[1]}"}
    if head in ("live", "live-heads") and len(parts) > 1:
        return {"runs", f"run:{parts[1].split('.')[0]}"}
    if k.startswith(_RUNS):
        return {"runs"} | ({"projects"} if head in ("deployments", "handoffs", "environments") else set())
    if head == "jobs" and len(parts) > 1:
        return {"jobs", f"job:{parts[1].split('.')[0]}"}
    if head == "notebooks":
        return {"notebooks"}
    if head == "compute":
        return {"compute"}
    if head == "serving":
        return {"compute", "deployed"}
    if k.startswith(_SETTINGS):
        return {"settings"}
    if head in ("team", "members", "invites", "collaboration"):
        return {"team"}
    if head in ("proposals", "recipes"):
        return {"projects"}
    if head == "objects":
        return set()  # blobs named by their hash: what points at one is written beside it, and bumps for it
    return {"data"}


# ---- the feed ------------------------------------------------------------------------------------------------------

class Feed:
    """The sequence, what changed at each step, and who is listening."""

    def __init__(self, keep: int = KEEP):
        # The sequence starts at the clock, in milliseconds, so one from before a restart is always older than
        # anything this process hands out, and its holder is told to read everything again.
        self.base = int(time.time() * 1000)
        self.seq = self.base
        self.log: collections.deque = collections.deque(maxlen=keep)
        self.cond = threading.Condition()
        self._listening: collections.Counter = collections.Counter()
        self._left: dict[str, float] = {}   # topics someone listened to a moment ago
        self._heard: list = []              # callbacks told of every bump, for a connection that stays open
        self.last_listener = 0.0

    def bump(self, *topics: str) -> int:
        """Something changed. Returns the new sequence number (or the current one when no topic was named)."""
        ts = {t for t in topics if t}
        with self.cond:
            if not ts:
                return self.seq
            self.seq += 1
            self.log.append((self.seq, frozenset(ts)))
            self.cond.notify_all()
            seq, heard = self.seq, list(self._heard)
        for fn in heard:
            try:
                fn(seq, ts)
            except Exception:  # noqa: BLE001  one closed connection never stops the others hearing
                pass
        return seq

    def hear(self, fn) -> None:
        """Call `fn(seq, topics)` on every bump, from the thread that bumped. For a WebSocket."""
        with self.cond:
            self._heard.append(fn)

    def unhear(self, fn) -> None:
        with self.cond:
            if fn in self._heard:
                self._heard.remove(fn)

    def _since(self, since: int, want: set[str] | None) -> tuple[list[str], bool]:
        """(the topics that changed after `since`, whether the client must read everything again). Lock held."""
        if since > self.seq or since < self.base:
            return [], True                      # from another run of the service
        if self.log and since < self.log[0][0] - 1 and since < self.seq:
            return [], True                      # further behind than the feed remembers
        hit: set[str] = set()
        for seq, ts in reversed(self.log):
            if seq <= since:
                break
            hit |= ts if want is None else (ts & want)
        return sorted(hit), False

    def answer(self, since: int | None, topics=None) -> dict:
        """What a client is told right now, without waiting."""
        want = None if topics is None else set(topics)
        with self.cond:
            if not since:
                return {"seq": self.seq, "changed": [], "at": _now()}
            changed, reset = self._since(int(since), want)
            return {"seq": self.seq, "changed": changed, "at": _now(), **({"reset": True} if reset else {})}

    def wait(self, since: int | None, topics=None, timeout: float = HOLD_S) -> dict:
        """Hold until one of `topics` changes after `since`, or `timeout` passes. With no `since` it answers at
        once with the current sequence, so a client reads everything once and then waits from there."""
        want = None if topics is None else set(topics)
        if not since:
            return self.answer(since, topics)
        end = time.monotonic() + max(0.0, float(timeout))
        self.listen(want or {"*"})
        try:
            with self.cond:
                while True:
                    changed, reset = self._since(int(since), want)
                    if changed or reset:
                        return {"seq": self.seq, "changed": changed, "at": _now(), **({"reset": True} if reset else {})}
                    left = end - time.monotonic()
                    if left <= 0:
                        return {"seq": self.seq, "changed": [], "at": _now()}
                    self.cond.wait(left)
        finally:
            self.unlisten(want or {"*"})

    # who is listening, so the watchers run only while someone is

    def listen(self, topics) -> None:
        with self.cond:
            self._listening.update(topics)
            self.last_listener = time.monotonic()
        _wake_watchers()

    def unlisten(self, topics) -> None:
        now = time.monotonic()
        with self.cond:
            self._listening.subtract(topics)
            self._listening += collections.Counter()  # drop what fell to zero
            for t in topics:
                self._left[t] = now
            self.last_listener = now

    def listening(self, prefix: str | None = None, linger: float = LINGER_S) -> bool:
        """Is anyone listening (to a topic that is `prefix` or starts with it, or to anything)? A long poll leaves
        and comes straight back, so a listener that left a moment ago still counts."""
        now = time.monotonic()
        with self.cond:
            self._left = {t: at for t, at in self._left.items() if now - at < linger}
            names = set(self._listening) | set(self._left)
        if prefix is None:
            return bool(names)
        return any(t == "*" or t == prefix or t.startswith(prefix) for t in names)


FEED = Feed()


def bump(*topics: str) -> int:
    return FEED.bump(*topics)


# ---- every write through this process ---------------------------------------------------------------------------------

_WATCHERS: dict[str, "Watcher"] = {}
_ROOTS: set[str] = set()   # the stores whose writes are this workspace's (the service names them: see `watch`)


def wrote(store, key: str) -> None:
    """Called by the storage layer after every successful write. Never raises: a feed that can't be told must
    never fail a write."""
    try:
        uri = getattr(store, "uri", "")
        if _ROOTS and uri not in _ROOTS:
            return  # a store that is not a workspace this service serves (a test's, a serving bucket's)
        ts = topics_for(key)
        w = _WATCHERS.get(uri)
        if w is not None:
            w.saw(key)  # this process wrote it: the folder watch need not say so a second time
        if ts:
            FEED.bump(*ts)
    except Exception:  # noqa: BLE001
        pass


# ---- writes made outside this process -------------------------------------------------------------------------------

class Watcher:
    """Watches one workspace on this computer for writes this process did not make: a `cst` command in a terminal,
    an agent. It looks at every folder's change time once a second, which is cheap (a folder's time moves when a
    file is added to it or replaced in it, and the stores only ever add or replace). A change is told within about
    a second. It runs only while someone is listening."""

    EVERY_S = 1.0
    SKIP = ("objects",)   # blobs named by their hash: nothing a screen lists

    def __init__(self, root: str, uri: str):
        self.root, self.uri = root, uri
        self.seen: dict[str, int] = {}
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.primed = False

    def _dirs(self):
        stack = [self.root]
        while stack:
            d = stack.pop()
            try:
                st = os.stat(d)
            except OSError:
                continue
            yield d, st.st_mtime_ns
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False) and not (d == self.root and e.name in self.SKIP) and not e.name.startswith(".tmp"):
                            stack.append(e.path)
            except OSError:
                continue

    def saw(self, key: str) -> None:
        """This process just wrote `key`: remember its folder's new time, so the scan does not report it again."""
        d = os.path.dirname(os.path.join(self.root, str(key).lstrip("/")))
        with self.lock:
            while d.startswith(self.root):
                try:
                    self.seen[d] = os.stat(d).st_mtime_ns
                except OSError:
                    pass
                if d == self.root:
                    break
                d = os.path.dirname(d)

    def scan(self) -> set[str]:
        """One look. Returns the topics of what changed since the last look (nothing on the first)."""
        now = dict(self._dirs())
        with self.lock:
            before, first = self.seen, not self.primed
            changed = [d for d, m in now.items() if before.get(d) != m]
            gone = [d for d in before if d not in now]
            self.seen, self.primed = now, True
        if first:
            return set()
        out: set[str] = set()
        for d in changed + gone:
            rel = os.path.relpath(d, self.root).replace(os.sep, "/")
            # A folder whose time moved had a file added or replaced in it. The file's name is not known from the
            # folder alone, so the folder stands for it ("projects/shop/" for "projects/shop/state.yaml").
            out |= topics_for("" if rel == "." else rel + "/x")
            if rel == ".":
                out |= {"projects", "settings", "data"}   # the workspace's own top files: projects.yaml, settings.yaml, library.yaml
        return out

    def run(self) -> None:
        while FEED.listening():
            t0 = time.monotonic()
            try:
                ts = self.scan()
                if ts:
                    FEED.bump(*ts)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(0.05, self.EVERY_S - (time.monotonic() - t0)))
        with self.lock:
            self.thread, self.primed = None, False   # the next listener starts from a fresh look

    def ensure(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.run, daemon=True, name="changes-watch")
            self.thread.start()


class BucketWatch:
    """A workspace kept in a bucket, written by another machine. Listing a bucket every second is neither cheap
    nor fast, so this reads the tags of its few index objects every two seconds while someone listens: the list of
    projects, each project's state, the settings, the library and the serving setup. A change to one of those is
    told within about two seconds. A dataset or a run written by another machine that touches none of them is
    seen when its project's state next changes, or when the screen is opened."""

    EVERY_S = 2.0
    INDEX = ("projects.yaml", "settings.yaml", "library.yaml", "serving/setup.yaml", "classes/classes.yaml")

    def __init__(self, store, uri: str):
        self.store, self.uri = store, uri
        self.tags: dict[str, str | None] = {}
        self.primed = False
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def saw(self, key: str) -> None:
        with self.lock:
            self.tags.pop(str(key).lstrip("/"), None)   # read again on the next look, without reporting it

    def _keys(self) -> list[str]:
        keys = list(self.INDEX)
        try:
            import yaml

            doc = yaml.safe_load(self.store.get("projects.yaml")) or {}
            rows = doc.get("projects") if isinstance(doc, dict) else doc
            keys += [f"projects/{(p.get('id') if isinstance(p, dict) else p)}/state.yaml" for p in rows or []]
        except Exception:  # noqa: BLE001
            pass
        return keys

    def scan(self) -> set[str]:
        out: set[str] = set()
        with self.lock:
            known, first = dict(self.tags), not self.primed
        now: dict[str, str | None] = {}
        for k in self._keys():
            try:
                now[k] = self.store.get_with_tag(k)[1]
            except Exception:  # noqa: BLE001  not there, or the bucket can't be reached just now
                now[k] = None
            if not first and k in known and known[k] != now[k]:
                out |= topics_for(k)
        with self.lock:
            self.tags, self.primed = now, True
        return out

    run = Watcher.run
    ensure = Watcher.ensure


def watch(store) -> None:
    """Name a workspace's store as one this service serves: its writes bump the feed, and writes made to it from
    outside this process are looked for while someone listens. Safe to call again."""
    uri = getattr(store, "uri", "")
    if not uri:
        return
    _ROOTS.add(uri)
    if uri not in _WATCHERS:
        root = getattr(store, "root", None)
        _WATCHERS[uri] = Watcher(root, uri) if root else BucketWatch(store, uri)
    if FEED.listening():
        _WATCHERS[uri].ensure()


_CLOUD_HOOKS: list = []   # called when someone starts listening: staging/cloud_watch.py registers itself here


def on_listen(fn) -> None:
    if fn not in _CLOUD_HOOKS:
        _CLOUD_HOOKS.append(fn)


def _wake_watchers() -> None:
    for w in list(_WATCHERS.values()):
        try:
            w.ensure()
        except Exception:  # noqa: BLE001
            pass
    for fn in list(_CLOUD_HOOKS):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def reset_for_tests() -> None:
    """A fresh feed with nothing watched. Tests only."""
    global FEED
    FEED = Feed()
    _WATCHERS.clear()
    _ROOTS.clear()
