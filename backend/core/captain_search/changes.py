"""One change feed for the app: what changed, the moment it changes.

Every screen used to keep itself current with its own timer (104 of them, at 1 to 60 seconds), and a row could
say "Stopping" for two hours after the model was gone. This is the one channel they listen to instead.

A process-wide sequence number and a set of coarse topics. Every write bumps the sequence and records which
topics changed. A client says which topics it shows and is told the moment one of them changes, then reads again
what it already reads. The feed carries no data: only "this changed".

    projects, project:<id>          data, table:<name>          runs, run:<id>          jobs, job:<id>
    weights, weights:<model>        weights-session:<id>
    deployed, deployment:<project>  notebooks                   logins      compute     settings     team
    pins (what a person gave their agent)                       views (the team's own screens, in the project folder)
    mlflow (the registered models were read again and are new)

A change to one thing bumps its own topic and its list's (project:shop and projects), so a list listens to the
plural and a page to its own.

Where bumps come from
- Every write through this process: the storage layer calls `wrote(store, key)` after each put, so no caller
  can forget (storage/backend.py). `topics_for` maps the key to its topics.
- State that is not a store write: a job's progress, a live run's events, a cloud login saved. Those call `bump`.
- Writes made outside this process (a `cst` command in a terminal, an agent, another machine): `Watcher`, while
  anyone is listening. A workspace on this computer is seen within about a second. One in a bucket within two to
  three, from the bucket's own listings (see `BucketWatch`), and the answer names what is only partly watched.
- What only a cloud knows (a model that stopped by itself): flow-clouds' watcher, which calls `bump` here and
  runs only while `listening("deployed")` or `listening("deployment:")`.

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
LINGER_S = 10.0      # how long a topic still counts as listened to after the last request for it ended: a long poll
                     # comes straight back, and a client that vanished simply stops asking


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
    if head == "weights-looks":
        return set()  # a screen's looks at a session, counted beside its record: no screen reads again for one
    if head == "weights":
        # weights/<model>/v<N>/... and weights/<model>/sessions/<id>/...: a model's numbers, and a session's record
        out = {"weights"} | ({f"weights:{parts[1]}"} if len(parts) > 2 else set())
        if len(parts) > 4 and parts[2] == "sessions":
            out.add(f"weights-session:{parts[3]}")
        return out
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

def covered(changed, want) -> set[str]:
    """The changed topics a client's list covers. A listed topic covers itself, and one that ends in a colon
    covers every topic that starts with it: "run:" is every run. No list covers everything."""
    if want is None:
        return set(changed)
    pre = tuple(w for w in want if w.endswith(":"))
    return {t for t in changed if t in want or (pre and t.startswith(pre))}


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
        self._asks: dict[str, int] = {}     # client -> its newest request: an older one of the same client lets go
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

    def _since(self, since: int, want: set[str] | None) -> tuple[list[dict], bool]:
        """(the topics that changed after `since`, each with the sequence of its latest change; whether the client
        must read everything again). Lock held."""
        if since > self.seq or since < self.base:
            return [], True                      # from another run of the service
        if self.log and since < self.log[0][0] - 1 and since < self.seq:
            return [], True                      # further behind than the feed remembers
        hit: dict[str, int] = {}
        for seq, ts in reversed(self.log):   # newest first, so the first time a topic is met is its latest change
            if seq <= since:
                break
            for t in covered(ts, want):
                hit.setdefault(t, seq)
        return [{"topic": t, "seq": hit[t]} for t in sorted(hit)], False

    def answer(self, since: int | None, topics=None) -> dict:
        """What a client is told right now, without waiting."""
        want = None if topics is None else set(topics)
        with self.cond:
            if not since:
                return {"seq": self.seq, "changed": [], "reset": False, "at": _now()}
            changed, reset = self._since(int(since), want)
            return {"seq": self.seq, "changed": changed, "reset": reset, "at": _now()}

    def touch(self, topics=None) -> None:
        """A client asked about these topics without waiting (it polls). It counts as listening for a while."""
        ts = set(topics) if topics else {"*"}
        self.listen(ts)
        self.unlisten(ts)

    def wait(self, since: int | None, topics=None, timeout: float = HOLD_S, client: str | None = None) -> dict:
        """Hold until one of `topics` changes after `since`, or `timeout` passes. With no `since` it answers at
        once with the current sequence, so a client reads everything once and then waits from there.

        A client cannot end a request it has open. So when one that names itself (`client`) asks again, for
        other topics, its older request answers at once with nothing: the new one is the one that counts."""
        want = None if topics is None else set(topics)
        mine = None
        if client:
            with self.cond:
                mine = self._asks[client] = self._asks.get(client, 0) + 1
                if len(self._asks) > 4096:
                    self._asks = {client: mine}
                self.cond.notify_all()
        if not since:
            return self.answer(since, topics)
        end = time.monotonic() + max(0.0, float(timeout))
        self.listen(want or {"*"})
        try:
            with self.cond:
                while True:
                    changed, reset = self._since(int(since), want)
                    if changed or reset:
                        return {"seq": self.seq, "changed": changed, "reset": reset, "at": _now()}
                    left = end - time.monotonic()
                    if left <= 0 or (mine is not None and self._asks.get(client) != mine):
                        return {"seq": self.seq, "changed": [], "reset": False, "at": _now()}
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

    def names(self, linger: float | None = None) -> set[str]:
        """The topics listened to now: a request open for them, or one that ended under ten seconds ago."""
        now = time.monotonic()
        linger = LINGER_S if linger is None else linger
        with self.cond:
            self._left = {t: at for t, at in self._left.items() if now - at < linger}
            return set(self._listening) | set(self._left)

    def listening(self, prefix: str | None = None, linger: float | None = None) -> bool:
        """Is anyone listening (to a topic that is `prefix` or starts with it, or to anything)? A long poll leaves
        and comes straight back, so a listener that left a moment ago still counts."""
        now = time.monotonic()
        linger = LINGER_S if linger is None else linger
        with self.cond:
            self._left = {t: at for t, at in self._left.items() if now - at < linger}
            names = set(self._listening) | set(self._left)
        if prefix is None:
            return bool(names)
        return any(t == "*" or t == prefix or t.startswith(prefix) for t in names)


FEED = Feed()


def bump(*topics: str) -> int:
    """Something changed under each topic. Safe from any thread, and cheap enough to call on every progress line.
    Returns the new sequence number."""
    return FEED.bump(*topics)


def listening(prefix: str | None = None) -> bool:
    """Is a request open for a topic that is `prefix` or starts with it, or did one end under 10 seconds ago?
    With no prefix: for any topic. What a watcher asks before it does work nobody would see."""
    return FEED.listening(prefix)


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

BUDGET = 0.02        # the most of one processor a watcher that has to look may use, averaged over its looks
MAX_GAP_S = 5.0      # the longest it waits between looks to stay inside that
MAX_DIRS = 8000      # folders one watcher holds open for the system's file events; past this it looks instead


def _gap(every: float, took: float, budget: float = BUDGET) -> float:
    """How long to wait after a look that took `took` seconds: the usual gap, stretched so that looking uses at
    most `budget` of one processor, up to MAX_GAP_S."""
    # A little under the budget: the work around a look (its thread waking, the feed told) is not in `took`.
    return min(MAX_GAP_S, max(every, took / (budget * 0.75), 0.05))


def _kinds(topics) -> set[str]:
    return {str(t).split(":")[0] for t in topics}


class Watcher:
    """Watches one workspace on this computer for writes this process did not make: a `cst` command in a terminal,
    an agent. It runs only while someone is listening, only for the folders their topics can come from, and it
    stops when the last listener has been gone ten seconds.

    Two ways, and which is in `mode`:
    - "events". Where the system has them (macOS and the BSDs: kqueue), each folder is held open and the system
      says when a file is added to it or replaced in it, which is all a store ever does. An idle workspace costs
      nothing: the thread sleeps until the system wakes it. A change is told within a few hundredths of a second.
    - "looks". Elsewhere, or past MAX_DIRS folders, every folder's change time is looked at about three times a
      second. Looking costs processor time in proportion to the workspace, so it has a budget: at most 2 percent
      of one processor. On a large workspace the wait between looks stretches, up to five seconds, and the feed's
      answer names its topics as only partly watched (`partial`), so a screen never says "Live" over a late read.
    """

    EVERY_S = 0.3
    SKIP = ("objects",)   # blobs named by their hash: nothing a screen lists
    SETTLE_S = 0.05       # after the system says a folder changed: long enough for this process to say it wrote it

    def __init__(self, root: str, uri: str):
        self.root, self.uri = root, uri
        self.seen: dict[str, int] = {}
        self.own: dict[str, float] = {}
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.primed = False
        self.mode = "idle"
        self.gap = self.EVERY_S
        self.looks = 0            # looks made (the "looks" mode), and events handled (the "events" mode): for tests
        self.poked = False        # a request just started listening: what is watched may need to follow
        self.tops: set[str] | None = None

    def _on(self) -> bool:
        """Someone is listening, and this is still the watcher of its workspace."""
        return FEED.listening() and _WATCHERS.get(self.uri) is self

    # ---- which folders the listeners' topics can come from

    def _wanted(self) -> set[str] | None:
        """The top folders worth watching for what is listened to now. None: all of them."""
        names = FEED.names()
        if "*" in names:
            return None
        want = _kinds(names)
        tops = set()
        try:
            with os.scandir(self.root) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False) and e.name not in self.SKIP and not e.name.startswith(".tmp"):
                        if _kinds(topics_for(f"{e.name}/a/b/c")) & want:
                            tops.add(e.name)
        except OSError:
            pass
        return tops

    def _top_wanted(self, name: str) -> bool:
        """Is this top folder one the listeners' topics can come from, as they are right now?"""
        if name in self.SKIP or name.startswith(".tmp"):
            return False
        names = FEED.names()
        return "*" in names or bool(_kinds(topics_for(f"{name}/a/b/c")) & _kinds(names))

    def _dirs(self, tops: set[str] | None = None):
        """The workspace's own folder, then every folder under the wanted top folders."""
        try:
            yield self.root, os.stat(self.root).st_mtime_ns
        except OSError:
            return
        stack = []
        try:
            with os.scandir(self.root) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False) and e.name not in self.SKIP and not e.name.startswith(".tmp") and (tops is None or e.name in tops):
                        stack.append(e.path)
        except OSError:
            return
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
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith(".tmp"):
                            stack.append(e.path)
            except OSError:
                continue

    def _topics_of(self, d: str) -> set[str]:
        rel = os.path.relpath(d, self.root).replace(os.sep, "/")
        # A folder that changed had a file added or replaced in it. The file's name is not known from the folder
        # alone, so the folder stands for it ("projects/shop/" for "projects/shop/state.yaml").
        if rel == ".":
            return {"projects", "settings", "data"}   # the workspace's own top files: projects.yaml, settings.yaml, library.yaml
        return topics_for(rel + "/x")

    def saw(self, key: str) -> None:
        """This process just wrote `key`: its folder's change time is remembered, so the watch does not tell it again."""
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

    def _mine(self, d: str, now: float = 0.0) -> bool:
        """Is this folder as this process last left it? Then the change the system reported was this process's own
        write, told when it was made. Judged by the folder's own change time, not by how recently this process
        wrote: another process's write a moment after ours moves the time on, and is told."""
        try:
            m = os.stat(d).st_mtime_ns
        except OSError:
            return False
        with self.lock:
            if self.seen.get(d) == m:
                return True
            self.seen[d] = m
            return False

    # ---- looking (the fallback)

    def scan(self) -> set[str]:
        """One look. Returns the topics of what changed since the last look (nothing on the first)."""
        self.looks += 1
        now = dict(self._dirs(self.tops))
        with self.lock:
            before, first = self.seen, not self.primed
            changed = [d for d, m in now.items() if before.get(d) != m]
            gone = [d for d in before if d not in now]
            self.seen, self.primed = now, True
        if first:
            return set()
        out: set[str] = set()
        for d in changed + gone:
            out |= self._topics_of(d)
        return out

    def _look_loop(self) -> None:
        self.mode = "looks"
        tops_at = 0.0
        while self._on():
            if self.poked or time.monotonic() - tops_at > 3.0:   # what is listened to changes
                self.poked = False
                tops, tops_at = self._wanted(), time.monotonic()
                if tops != self.tops:
                    with self.lock:
                        self.tops, self.primed = tops, False   # other folders: the next look is a first look
            t0 = time.monotonic()
            try:
                ts = self.scan()
                if ts:
                    FEED.bump(*ts)
            except Exception:  # noqa: BLE001
                pass
            self.gap = _gap(self.EVERY_S, time.monotonic() - t0)
            end = time.monotonic() + self.gap
            while time.monotonic() < end and self._on():
                time.sleep(min(0.25, max(0.0, end - time.monotonic())))

    # ---- the system's file events

    def _events_loop(self) -> bool:
        """Hold each wanted folder open and sleep until the system says one changed. False when this can't be done
        here (no kqueue, too many folders, the system refused): the caller looks instead."""
        import select

        if not hasattr(select, "kqueue"):
            return False
        try:
            import resource

            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            need = MAX_DIRS + 512
            if soft < need:
                resource.setrlimit(resource.RLIMIT_NOFILE, (min(need, hard) if hard != resource.RLIM_INFINITY else need, hard))
        except Exception:  # noqa: BLE001
            pass
        flags_open = getattr(os, "O_EVTONLY", os.O_RDONLY)
        note = select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_EXTEND
        kq = select.kqueue()
        fds: dict[int, str] = {}
        dirs: dict[str, int] = {}

        def add(d: str) -> bool:
            if d in dirs:
                return True
            if len(dirs) >= MAX_DIRS:
                return False
            try:
                fd = os.open(d, flags_open)
                kq.control([select.kevent(fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR, note)], 0, 0)
            except OSError:
                return False
            fds[fd], dirs[d] = d, fd
            return True

        def drop(d: str) -> None:
            fd = dirs.pop(d, None)
            if fd is not None:
                fds.pop(fd, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

        def sync() -> bool:
            tops = self._wanted()
            if tops == self.tops and dirs:
                return True
            self.tops = tops
            keep = set()
            for d, _ in self._dirs(tops):
                keep.add(d)
                if not add(d):
                    return False
            for d in [d for d in dirs if d not in keep]:
                drop(d)
            return True

        try:
            if not sync():
                return False
            self.mode = "events"
            synced = time.monotonic()
            while self._on():
                try:
                    evs = kq.control(None, 256, 0.25)
                except OSError:
                    return False
                # What is listened to changes: the folders held open follow it, at once when a request starts
                # listening (`ensure` says so), and every few seconds besides.
                if self.poked or time.monotonic() - synced > 3.0:
                    self.poked, synced = False, time.monotonic()
                    if not sync():
                        return False
                if not evs:
                    continue
                time.sleep(self.SETTLE_S)                  # this process's own write says so in that time (`saw`)
                try:
                    evs += kq.control(None, 256, 0)        # what else came meanwhile, told with it
                except OSError:
                    pass
                self.looks += 1
                now, out = time.monotonic(), set()
                for ev in evs:
                    d = fds.get(ev.ident)
                    if d is None:
                        continue
                    if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME) and not os.path.isdir(d):
                        drop(d)
                        out |= self._topics_of(d)
                        continue
                    # A folder made inside it is held open too, and what is already in it is told.
                    try:
                        with os.scandir(d) as it:
                            new = [e.path for e in it if e.is_dir(follow_symlinks=False) and e.path not in dirs and not e.name.startswith(".tmp")
                                   and (d != self.root or self._top_wanted(e.name))]
                    except OSError:
                        new = []
                    while new:
                        n = new.pop()
                        if not add(n):
                            return False
                        if not self._mine(n, now):
                            out |= self._topics_of(n)
                        try:
                            with os.scandir(n) as it:
                                new += [e.path for e in it if e.is_dir(follow_symlinks=False) and e.path not in dirs]
                        except OSError:
                            pass
                    if not self._mine(d, now):
                        out |= self._topics_of(d)
                if out:
                    FEED.bump(*out)
            return True
        finally:
            for fd in list(fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                kq.close()
            except OSError:
                pass

    def partial(self) -> tuple[set[str], set[str]]:
        """(topic names, topic prefixes) only partly watched: everything this workspace's folders feed, while the
        watch has had to slow its looks to stay inside its budget. Nothing while it is told by the system, or
        looks often."""
        if self.mode != "looks" or self.gap <= 1.0:
            return set(), set()
        names, prefixes = {"projects", "settings", "data"}, set()
        try:
            tops = [e.name for e in os.scandir(self.root) if e.is_dir() and e.name not in self.SKIP]
        except OSError:
            tops = []
        for top in tops:
            for t in topics_for(f"{top}/a/b/c"):
                (prefixes.add(t.split(":")[0] + ":") if ":" in t else names.add(t))
        return names, prefixes

    def run(self) -> None:
        try:
            if not self._events_loop():
                self._look_loop()
        except Exception:  # noqa: BLE001  the watch never takes the service down
            pass
        with self.lock:
            self.thread, self.primed, self.mode, self.tops = None, False, "idle", None   # the next listener starts afresh
            self.seen, self.own, self.gap = {}, {}, self.EVERY_S

    def ensure(self) -> None:
        self.poked = True
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.run, daemon=True, name="changes-watch")
            self.thread.start()


class BucketWatch:
    """A workspace kept in a bucket, written by another machine. A bucket has no folder times to look at, but its
    listing carries every object's tag, so one listing of a folder shows what was added, replaced or removed
    under it without reading any object. While someone listens, every two seconds this lists:

    - the workspace's top level (the project list, the settings, the library),
    - every folder there except `objects/` (blobs named by their hash) and `staging/`,
    - under `staging/`, each deployed model's own level (its deploy and control documents).

    A change is told in about two to three seconds. The registry keeps no one index object for datasets, runs,
    jobs or notebooks, which is why their folders are listed whole: a new version is a new key, and a record
    replaced in place has a new tag.

    What it does not watch, and says so (`partial`): what a deployed model writes about itself (its request log,
    its copies, its health checks), which is many small objects. And a folder whose listing is slow (over a second)
    or large (over 5,000 objects) is listed every 30 seconds instead, and its topics are named as partly watched.
    A look never takes more than half the time: the wait is at least as long as the look took."""

    EVERY_S = 2.0
    SLOW_EVERY_S = 30.0
    SLOW_AFTER_S = 1.0
    SLOW_KEYS = 5000
    DIRS_EVERY_S = 30.0          # how often the list of deployed models' folders is read again
    SKIP = ("objects/",)
    OWN_S = 60.0                 # how long this process's own write is remembered, so it is not told twice

    def __init__(self, store, uri: str):
        self.store, self.uri = store, uri
        self.tags: dict[tuple, dict[str, str]] = {}     # (prefix, shallow) -> {key: tag} at the last look
        self.slow: dict[str, float] = {}                # folder -> when it may be listed next
        self.envs: list[str] = []                       # "staging/<project>/<env>/"
        self.envs_at = 0.0
        self.own: dict[str, float] = {}
        self.primed = False
        self.looks = 0                                  # listings made, for the tests
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def saw(self, key: str) -> None:
        with self.lock:
            self.own[str(key).lstrip("/")] = time.monotonic()

    def _list(self, prefix: str, shallow: bool) -> dict[str, str] | None:
        self.looks += 1
        try:
            return dict(self.store.list_tags(prefix, shallow))
        except Exception:  # noqa: BLE001  the bucket can't be reached just now: nothing is said, the next look tries
            return None

    def _diff(self, prefix: str, shallow: bool, now: dict[str, str]) -> set[str]:
        """The keys added, replaced or removed under one listing since the last look at it."""
        before = self.tags.get((prefix, shallow))
        self.tags[(prefix, shallow)] = now
        if before is None:
            # A folder that was not there at the last look: all of it is new. On the first look nothing is.
            return set(now) if self.primed else set()
        return {k for k in set(now) | set(before) if now.get(k) != before.get(k)}

    def scan(self) -> set[str]:
        t = time.monotonic()
        moved: set[str] = set()
        top = self._list("", True)
        if top is None:
            return set()
        moved |= self._diff("", True, top)
        for d in sorted(k for k in top if k.endswith("/") and k not in self.SKIP and k != "staging/"):
            if self.slow.get(d, 0.0) > t:
                continue
            t0 = time.monotonic()
            now = self._list(d, False)
            if now is None:
                continue
            if time.monotonic() - t0 > self.SLOW_AFTER_S or len(now) > self.SLOW_KEYS:
                self.slow[d] = time.monotonic() + self.SLOW_EVERY_S
            else:
                self.slow.pop(d, None)
            moved |= self._diff(d, False, now)
        for (d, shallow), had in list(self.tags.items()):
            if d and not shallow and d not in top:   # a folder whose last object was removed is no longer listed at all
                moved |= set(had)
                del self.tags[(d, shallow)]
        if "staging/" in top:
            if not self.envs or t - self.envs_at > self.DIRS_EVERY_S or any(k.startswith("staging/") for k in moved):
                envs = []
                for pd in [k for k in (self._list("staging/", True) or {}) if k.endswith("/")]:
                    envs += [k for k in (self._list(pd, True) or {}) if k.endswith("/")]
                new = [e for e in envs if e not in self.envs]
                self.envs, self.envs_at = envs, t
                if self.primed and new:
                    moved |= {e + "deploy.json" for e in new}   # a model deployed from another machine for the first time
            for e in self.envs:
                now = self._list(e, True)
                if now is not None:
                    moved |= self._diff(e, True, now)
        with self.lock:
            now_t = time.monotonic()
            self.own = {k: at for k, at in self.own.items() if now_t - at < self.OWN_S}
            mine = set(self.own)
            first, self.primed = not self.primed, True
        if first:
            return set()
        out: set[str] = set()
        for k in moved:
            if k.endswith("/") or k in mine:
                continue   # a folder is told by what is in it, and this process's own write was told when made
            out |= topics_for(k)
        return out

    def partial(self) -> tuple[set[str], set[str]]:
        """(topic names, topic prefixes) that are only partly watched here, so a screen keeps a slow read of its
        own for them."""
        names, prefixes = {"deployed"}, {"deployment:"}
        t = time.monotonic()
        for d, until in list(self.slow.items()):
            if until > t - self.SLOW_EVERY_S:
                for topic in topics_for(d + "x/x"):
                    (prefixes.add(topic.split(":")[0] + ":") if ":" in topic else names.add(topic))
        return names, prefixes

    def run(self) -> None:
        while FEED.listening():
            t0 = time.monotonic()
            try:
                ts = self.scan()
                if ts:
                    FEED.bump(*ts)
            except Exception:  # noqa: BLE001
                pass
            took = time.monotonic() - t0
            end = time.monotonic() + max(self.EVERY_S - took, took, 0.05)
            while time.monotonic() < end and FEED.listening():
                time.sleep(min(0.2, max(0.0, end - time.monotonic())))
        with self.lock:
            self.thread, self.primed = None, False
            self.tags, self.envs = {}, []

    ensure = Watcher.ensure


def partial(store, topics=None) -> list[str]:
    """Which of `topics` (or which topics at all) are only partly watched for this workspace: changes to them
    made outside this service can be missed or told late, so a screen keeps a slow read of its own for them.
    Nothing for a workspace on this computer."""
    w = _WATCHERS.get(getattr(store, "uri", ""))
    fn = getattr(w, "partial", None)
    if not callable(fn):
        return []
    names, prefixes = fn()
    if topics is None:
        return sorted(names | {p + "*" for p in prefixes})
    return sorted(t for t in set(topics) if t in names or any(t.startswith(p) for p in prefixes))


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


class FolderWatch:
    """One folder on this computer that is not in the workspace, written by other processes: the live runs' folder,
    the notebooks' drafts. While someone listens to one of `listens`, its files' change times are looked at about
    three times a second, and `topics(name)` says what a changed file tells. `mine(name)` is true for a file this process just
    wrote and told about itself."""

    def __init__(self, path, topics, listens: tuple, mine=None, deep: bool = False):
        self.path, self.topics, self.listens, self.mine, self.deep = path, topics, tuple(listens), mine, deep
        self.seen: dict[str, int] = {}
        self.primed = False
        self.thread: threading.Thread | None = None

    def wanted(self) -> bool:
        return any(FEED.listening(t) for t in self.listens)

    def look(self) -> set[str]:
        now: dict[str, int] = {}
        try:
            root = self.path()
            if not root:
                now = {}
            elif self.deep:
                # Every file under it, by its own change time: an editor that saves in place moves the file's time,
                # not its folder's. For a small folder of code (a team's views), never a workspace.
                for d, dirs, files in os.walk(root):
                    dirs[:] = [x for x in dirs if not x.startswith(".") and x != "node_modules"]
                    for name in files:
                        p = os.path.join(d, name)
                        try:
                            now[os.path.relpath(p, root)] = os.stat(p).st_mtime_ns
                        except OSError:
                            pass
                    if len(now) > 5000:
                        break
            else:
                with os.scandir(root) as it:
                    for e in it:
                        if not e.name.endswith(".tmp"):
                            now[e.name] = e.stat().st_mtime_ns
        except OSError:
            now = {}
        before, first = self.seen, not self.primed
        self.seen, self.primed = now, True
        out: set[str] = set()
        if first:
            return out
        for name in set(now) | set(before):
            if now.get(name) != before.get(name) and not (self.mine and self.mine(name)):
                out |= set(self.topics(name) or ())
        return out

    def run(self) -> None:
        while self.wanted():
            t0 = time.monotonic()
            try:
                ts = self.look()
                if ts:
                    FEED.bump(*ts)
            except Exception:  # noqa: BLE001
                pass
            # The four folders outside the workspace share the budget: a quarter each. A folder with thousands of
            # files in it (years of live runs) is looked at less often, never harder.
            time.sleep(_gap(0.3, time.monotonic() - t0, BUDGET / 4))
        self.thread, self.primed = None, False

    def ensure(self) -> None:
        if (self.thread is not None and self.thread.is_alive()) or not self.wanted():
            return
        self.thread = threading.Thread(target=self.run, daemon=True, name="changes-folder")
        self.thread.start()


_FOLDERS: list[FolderWatch] = []


def watch_folder(path, topics, listens: tuple, mine=None, deep: bool = False) -> FolderWatch:
    """Look at a folder outside the workspace while someone listens to `listens`. `path()` gives the folder, or
    nothing when there is none just now. `deep` looks at every file under it."""
    w = FolderWatch(path, topics, listens, mine, deep)
    _FOLDERS.append(w)
    return w


_CLOUD_HOOKS: list = []   # called each time a request starts listening, so a watcher starts at once


def on_listen(fn) -> None:
    """Call `fn()` each time a request starts listening (not only the first), from that request's thread. It
    must be quick and safe to call often."""
    if fn not in _CLOUD_HOOKS:
        _CLOUD_HOOKS.append(fn)


def _wake_watchers() -> None:
    for w in list(_WATCHERS.values()) + list(_FOLDERS):
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
