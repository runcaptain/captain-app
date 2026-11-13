"""Captain's logs: the services' own log, and reading and bundling every log the app keeps.

The desktop app writes `app.log`, crash records (`crash-<UTC time>.log`) and `last-run.json` into one folder
(crates/captain-logs in the app's source). `cst serve` writes `services.log` beside them, in the same shape:

    2026-11-13T10:22:33.123Z INFO  services  started: cst 0.1.0, pid 4100

One line per event, further lines of an event (a traceback) indented. A new file every 5 MB, five old ones kept.

What is never written: Authorization headers, tokens, keys, passwords, request or response bodies, file contents,
query strings and query text, sign-in codes. `redact` takes anything shaped like a key out of every line, even inside
an exception's message, and writes a home folder as `~`. A log may be pasted into a bug report. The rules are the
same as the app's (crates/captain-logs/src/redact.rs); both are checked against tests/fixtures/redaction-cases.json.
"""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import traceback
import uuid
import zipfile

DIR_ENV = "CAPTAIN_LOG_DIR"
MAX_BYTES = 5 * 1024 * 1024
KEEP = 5
TAIL_LINES = 200
SLOW_SECONDS = 2.0
MARK = "[redacted]"
# Requests that wait on purpose (the change feed holds a request open until something changes): never "slow".
WAITS = ("/api/changes",)

LOG = logging.getLogger("captain.services")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def log_dir() -> str:
    """The folder the app and the services log into. $CAPTAIN_LOG_DIR wins (the app sets it for the services it
    starts and for its terminals). Otherwise the platform's usual place."""
    d = os.environ.get(DIR_ENV)
    if d:
        return d
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Logs", "Captain")
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"), "Captain", "Logs")
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), "captain-app", "logs")


# ---- redaction ------------------------------------------------------------------------------

_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_HEADER = re.compile(r"""(?i)\b((?:proxy-)?authorization|(?:set-)?cookie)(["']?\s*[:=]\s*)[^\r\n]*""")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_PAIR = re.compile(r"""(?i)\b([a-z0-9_.-]*(?:key|token|secret|password|passwd|pwd|passphrase|credentials?|signature))(["']?\s*[:=]\s*)(\[redacted\]|"[^"]*"|'[^']*'|[^\s"'&,;)}\]]+)""")
_CODE = re.compile(r"""(?i)\b((?:user|device|auth|authorization|verification|sign_?in|login|one_time)_code|otp|code_verifier)(["']?\s*[:=]\s*["']?)[A-Za-z0-9-]{4,}|\b(code)(["']?\s*[:=]\s*["']?)(?:\d{6,8}|[A-Za-z0-9]{4}-[A-Za-z0-9]{4})\b""")
_AWS = re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|ABIA|ACCA)[A-Z0-9]{16}\b")
_PREFIXED = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}")
_RUN = re.compile(r"[A-Za-z0-9_+=-]{32,}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HOME = re.compile(r"""(?:/Users|/home|(?i:[a-z]:\\Users))[/\\][^/\\\s:"']+""")
_HEX = re.compile(r"[0-9a-fA-F]+\Z")
# A database error quotes the statement and its values: query text, which a log never holds.
_SQL = re.compile(r"\[(SQL|parameters): .*?\](?=\s|$)", re.S)


def _looks_secret(run: str) -> bool:
    """A long run is a secret when it is all hex (a hash, a key) or mixes digits with both cases (base64)."""
    if _HEX.match(run):
        return True
    return any(c.isdigit() for c in run) and any("A" <= c <= "Z" for c in run) and any("a" <= c <= "z" for c in run)


def _code(m: "re.Match") -> str:
    name, sep = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
    return f"{name}{sep}{MARK}"


def redact(text: str, home: str | None = None) -> str:
    """`text` with secrets replaced by `[redacted]`, email addresses by `[email]`, and home folders by `~`."""
    s = str(text)
    home = (home or "").rstrip("/\\")
    if len(home) > 1:
        s = s.replace(home, "~")
    s = _HOME.sub("~", s)
    s = _USERINFO.sub(rf"\g<1>{MARK}@", s)
    s = _HEADER.sub(rf"\g<1>\g<2>{MARK}", s)
    s = _BEARER.sub(rf"\g<1> {MARK}", s)
    s = _PAIR.sub(rf"\g<1>\g<2>{MARK}", s)
    s = _CODE.sub(_code, s)
    s = _AWS.sub(MARK, s)
    s = _PREFIXED.sub(MARK, s)
    s = _RUN.sub(lambda m: MARK if _looks_secret(m.group(0)) else m.group(0), s)
    return _EMAIL.sub("[email]", s)


def _home() -> str | None:
    h = os.path.expanduser("~")
    return h if h and h != "~" else None


def describe(exc: BaseException) -> str:
    """An exception as `Type: message`: one line, no quoted SQL, at most 500 characters."""
    msg = _SQL.sub(lambda m: f"[{m.group(1)}: left out]", str(exc))
    msg = " ".join(msg.split())
    if len(msg) > 500:
        msg = msg[:500] + " ..."
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def trace(exc: BaseException) -> str:
    """Where an exception came from: the frames (file, line, the code there), then `Type: message` through
    `describe`. Not `traceback.format_exception`, whose last line repeats the raw message."""
    lines: list[str] = []
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if lines:
            lines.append("caused by:")
        lines += [ln.rstrip("\n") for ln in traceback.format_tb(cur.__traceback__)]
        lines.append(describe(cur))
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    return "\n".join(lines)


# ---- writing --------------------------------------------------------------------------------

def stamp(t: float | None = None) -> str:
    d = _dt.datetime.fromtimestamp(time.time() if t is None else t, _dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


class _Format(logging.Formatter):
    """`<UTC time> <LEVEL> <source>  <message>`, redacted; further lines indented."""

    def format(self, record: logging.LogRecord) -> str:
        level = {"WARNING": "WARN", "CRITICAL": "ERROR"}.get(record.levelname, record.levelname)
        source = getattr(record, "source", "services")
        lines = redact(record.getMessage(), _home()).rstrip().split("\n")
        head = f"{stamp(record.created)} {level:<5} {source}  {lines[0]}"
        return "\n".join([head] + ["    " + ln.rstrip() for ln in lines[1:]])


_handler: logging.Handler | None = None
_lock = threading.Lock()


def start(folder: str | None = None, name: str = "services.log") -> str:
    """Open the services' log in `folder` (default: `log_dir()`). Safe to call twice. Returns the file's path."""
    global _handler
    folder = folder or log_dir()
    path = os.path.join(folder, name)
    with _lock:
        if _handler is not None:
            LOG.removeHandler(_handler)
            _handler.close()
        os.makedirs(folder, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=KEEP, encoding="utf-8")
        h.setFormatter(_Format())
        LOG.addHandler(h)
        LOG.setLevel(logging.INFO)
        _handler = h
    return path


def stop() -> None:
    global _handler
    with _lock:
        if _handler is not None:
            LOG.removeHandler(_handler)
            _handler.close()
            _handler = None


def info(source: str, message: str) -> None:
    LOG.info(message, extra={"source": source})


def warn(source: str, message: str) -> None:
    LOG.warning(message, extra={"source": source})


def error(source: str, message: str, exc: BaseException | None = None) -> None:
    LOG.error(message + ("\n" + trace(exc) if exc is not None else ""), extra={"source": source})


# ---- requests -------------------------------------------------------------------------------

class _Count:
    """Requests that went well are counted, not listed: one line a minute says how many."""

    def __init__(self):
        self.lock = threading.Lock()
        self.since = time.time()
        self.ok = 0
        self.refused = 0

    def add(self, status: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            if now - self.since >= 60 and (self.ok or self.refused):
                self._flush(now)
            elif now - self.since >= 60:
                self.since = now
            if status < 400:
                self.ok += 1
            elif status < 500:
                self.refused += 1

    def _flush(self, now: float) -> None:
        info("requests", f"{self.ok} requests answered and {self.refused} refused (4xx) since {stamp(self.since)}")
        self.since, self.ok, self.refused = now, 0, 0

    def flush(self) -> None:
        with self.lock:
            if self.ok or self.refused:
                self._flush(time.time())


COUNT = _Count()


def install(app) -> None:
    """Log what goes wrong with a request: every 5xx (method, path, status, a request id, and the exception with
    its traceback when there is one), and any request slower than two seconds. The path is logged without its
    query string, and nothing of a body or a header. Every response carries its id in X-Request-Id."""

    @app.middleware("http")
    async def _log_requests(request, call_next):
        rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        began = time.monotonic()
        where = f"{request.method} {request.url.path}"
        try:
            response = await call_next(request)
        except Exception as exc:  # no handler took it: the client gets a bare 500
            error("requests", f"{where} 500 id={rid} after {time.monotonic() - began:.2f} s", exc)
            raise
        took = time.monotonic() - began
        response.headers["X-Request-Id"] = rid
        if response.status_code >= 500:
            error("requests", f"{where} {response.status_code} id={rid} after {took:.2f} s", getattr(request.state, "failure", None))
        elif took > SLOW_SECONDS and not request.url.path.startswith(WAITS):
            warn("requests", f"slow: {where} {response.status_code} id={rid} took {took:.2f} s")
        COUNT.add(response.status_code)
        return response


def note_failure(request, exc: BaseException) -> None:
    """An exception handler that answers with a 5xx tells the log which exception it was."""
    try:
        request.state.failure = exc
    except Exception:  # noqa: BLE001
        pass


def watch_threads() -> None:
    """An exception nothing caught, in a thread or at the top: logged before the default hook prints it."""
    before_thread, before_top = threading.excepthook, sys.excepthook

    def in_thread(args):
        if args.exc_type is not SystemExit and args.exc_value is not None:
            error("threads", f"uncaught exception in thread {getattr(args.thread, 'name', '?')}", args.exc_value)
        before_thread(args)

    def at_top(kind, value, tb):
        if value is not None and not isinstance(value, (KeyboardInterrupt, SystemExit)):
            error("services", "uncaught exception; the services are stopping", value)
        before_top(kind, value, tb)

    threading.excepthook, sys.excepthook = in_thread, at_top


# ---- reading --------------------------------------------------------------------------------

_SINCE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(s|m|h|d|w)\s*\Z")
_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) ")


def since_seconds(text: str) -> float:
    """`90s`, `30m`, `1h`, `2d`, `1w` as seconds. Raises ValueError on anything else."""
    m = _SINCE.match(text or "")
    if not m:
        raise ValueError(f"--since takes a number and a unit, like 30m, 1h or 2d (not {text!r})")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def _events(path: str) -> list[tuple[str, str]]:
    """A log file as `(time, text)` events: a line that starts with a time starts one, the lines under it belong
    to it. Lines before the first time (there should be none) get an empty time."""
    out: list[tuple[str, list[str]]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = _TIME.match(line)
                if m or not out:
                    out.append((m.group(1) if m else "", [line]))
                else:
                    out[-1][1].append(line)
    except OSError:
        return []
    return [(t, "\n".join(ls)) for t, ls in out]


def _files(folder: str, name: str, old: int = KEEP) -> list[str]:
    """`name` and its older files, oldest first, those that exist."""
    paths = [os.path.join(folder, f"{name}.{n}") for n in range(old, 0, -1)] + [os.path.join(folder, name)]
    return [p for p in paths if os.path.isfile(p)]


def crash_records(folder: str) -> list[str]:
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith("crash-") and n.endswith(".log"))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names]


def read(which: str = "all", since: float | None = None, folder: str | None = None, now: float | None = None) -> str:
    """The logs as text. `which`: app, services, crash or all (app and services merged by time, then the crash
    records). `since`: only the last so many seconds."""
    folder = folder or log_dir()
    cut = stamp((time.time() if now is None else now) - since) if since else ""
    parts: list[str] = []
    events: list[tuple[str, str]] = []
    for name in (("app.log",) if which == "app" else ("services.log",) if which == "services" else ("app.log", "services.log") if which == "all" else ()):
        for p in _files(folder, name):
            events += _events(p)
    if which == "all":
        events.sort(key=lambda e: e[0])  # stable: equal times keep their file order
    parts += [text for t, text in events if not cut or t >= cut]
    if which in ("crash", "all"):
        for p in crash_records(folder):
            if since and os.path.getmtime(p) < (time.time() if now is None else now) - since:
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    parts.append(f"==== {os.path.basename(p)} ====\n{f.read().rstrip()}")
            except OSError:
                pass
    return "\n".join(parts) + ("\n" if parts else "")


def follow(which: str, folder: str | None = None, out=None, poll: float = 0.5, rounds: int | None = None) -> None:
    """Print new lines of app.log and services.log as they are written, until interrupted. A log that started a
    new file is picked up from its beginning. `rounds` ends it after so many looks (tests)."""
    folder = folder or log_dir()
    out = out or sys.stdout
    names = [n for n, w in (("app.log", "app"), ("services.log", "services")) if which in (w, "all")]
    at: dict[str, int] = {}
    for n in names:
        try:
            at[n] = os.path.getsize(os.path.join(folder, n))
        except OSError:
            at[n] = 0
    looked = 0
    while rounds is None or looked < rounds:
        looked += 1
        for n in names:
            p = os.path.join(folder, n)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < at[n]:
                at[n] = 0
            if size > at[n]:
                with open(p, "rb") as f:
                    f.seek(at[n])
                    chunk = f.read()
                keep = chunk.rfind(b"\n") + 1  # never half a line
                out.write(chunk[:keep].decode("utf-8", "replace"))
                out.flush()
                at[n] += keep
        if rounds is None or looked < rounds:
            time.sleep(poll)


# ---- a bundle for a bug report --------------------------------------------------------------

def last_run(folder: str | None = None) -> dict:
    """How the app's last run ended, as the app found it at its latest start (`last-run.json`)."""
    import json

    try:
        with open(os.path.join(folder or log_dir(), "last-run.json"), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def facts(folder: str | None = None) -> str:
    """A few lines about this installation, for the top of a bug report: versions, the system, how the last run
    ended. No names, no paths outside the log folder."""
    from . import __version__

    folder = folder or log_dir()
    started = ""
    for p in reversed(_files(folder, "app.log")):
        for t, text in reversed(_events(p)):
            if " app  started: " in text:
                started = text.split(" app  started: ", 1)[1].split("\n")[0]
                break
        if started:
            break
    last = last_run(folder)
    lines = [
        "Captain logs",
        f"made: {stamp()}",
        f"app: {started or 'unknown (no start line in app.log)'}",
        f"services (cst): {__version__}",
        f"system: {platform.system()} {platform.release()} {platform.machine()}, Python {platform.python_version()}",
        f"last run: {last.get('detail') or 'unknown (the app has not recorded one here)'}",
    ]
    if last.get("record"):
        lines.append(f"crash record: {last['record']}")
    lines.append(f"log folder: {folder}")
    return redact("\n".join(lines) + "\n", _home())


def bundle(to: str | None = None, folder: str | None = None) -> str:
    """One zip with app.log, services.log (each with its older files), the crash records, installs.log, how the
    last run ended, and facts.txt. Everything is redacted once more on the way in. Returns the zip's path.
    `to`: the folder to write it in (default: $CAPTAIN_BUNDLE_DIR, else the Desktop, else the home folder)."""
    folder = folder or log_dir()
    to = to or os.environ.get("CAPTAIN_BUNDLE_DIR")
    if not to:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        to = desktop if os.path.isdir(desktop) else os.path.expanduser("~")
    os.makedirs(to, exist_ok=True)
    name = "captain-logs-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(to, name + ".zip")
    n = 2
    while os.path.exists(path):
        path = os.path.join(to, f"{name}-{n}.zip")
        n += 1
    members = _files(folder, "app.log") + _files(folder, "services.log") + crash_records(folder)[-20:]
    members += [p for p in (os.path.join(folder, x) for x in ("installs.log", "last-run-tail.log", "last-run.json")) if os.path.isfile(p)]
    home = _home()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}/facts.txt", facts(folder))
        for p in members:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    z.writestr(f"{name}/{os.path.basename(p)}", redact(f.read(), home))
            except OSError:
                continue
    return path


def what_happened(folder: str | None = None) -> str:
    """The crash record of the last run when it crashed, the end of its log when it stopped without one, and
    otherwise the last 200 lines of app.log."""
    folder = folder or log_dir()
    last = last_run(folder)

    def text(name: str) -> str:
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    if last.get("ending") == "crashed" and last.get("record") and text(last["record"]):
        return text(last["record"])
    if last.get("ending") in ("signal", "vanished") and text("last-run-tail.log"):
        return text("last-run-tail.log")
    lines: list[str] = []
    for p in _files(folder, "app.log", 1):
        with open(p, encoding="utf-8", errors="replace") as f:
            lines += f.read().splitlines()
    return "Captain: the last run ended normally. These are the last lines of app.log.\n\n" + "\n".join(lines[-TAIL_LINES:]) + "\n"
