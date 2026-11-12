"""`cst` run as a program ends the moment its work is printed, with the right exit code, and never waits on shutdown."""
from __future__ import annotations

import subprocess
import sys

RUN = [sys.executable, "-c", "import sys; from captain_search.cli.main import app; sys.argv[0] = 'cst'; app()"]


def _cst(*args, home):
    return subprocess.run([*RUN, *args], capture_output=True, text=True, timeout=120,
                          env={"CST_HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})


def test_output_reaches_a_pipe_and_the_exit_code_is_kept(tmp_path):
    ok = _cst("--help", home=tmp_path)
    assert ok.returncode == 0 and "Captain Search Tools" in ok.stdout  # printed in full before the hard exit
    made = _cst("init", "exit-test", "--storage", str(tmp_path / "store"), home=tmp_path)
    assert made.returncode == 0 and "exit-test" in made.stdout
    bad = _cst("projects", "staging", "no-such-project", home=tmp_path)
    assert bad.returncode == 1 and "no project" in bad.stderr
    usage = _cst("projects", "no-such-command", home=tmp_path)
    assert usage.returncode == 2 and "No such command" in usage.stderr


def test_a_crash_still_prints_its_trace_and_exits_1(tmp_path):
    r = subprocess.run([sys.executable, "-c",
                        "import sys; from captain_search.cli import main as M\n"
                        "@M.app.command('boom-for-test', hidden=True)\n"
                        "def boom():\n    raise RuntimeError('kaboom')\n"
                        "sys.argv = ['cst', 'boom-for-test']; M.app()"],
                       capture_output=True, text=True, timeout=120, env={"CST_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 1 and "kaboom" in r.stderr


def test_embedding_the_app_with_arguments_is_untouched():
    from typer.testing import CliRunner

    from captain_search.cli.main import app

    out = CliRunner().invoke(app, ["--help"])  # returns to the caller: no hard exit when it is called with arguments
    assert out.exit_code == 0 and "Captain Search Tools" in out.output


def test_work_on_a_thread_and_exit_hooks_finish_before_the_exit(tmp_path):
    """The hard exit must not cut off a writer: a thread that is not a daemon is waited for, and exit hooks run."""
    out = tmp_path / "written.txt"
    r = subprocess.run([sys.executable, "-c",
                        "import sys, time, threading, atexit; from captain_search.cli import main as M\n"
                        f"P = {str(out)!r}\n"
                        "def slow():\n    time.sleep(1.0)\n    open(P, 'a').write('thread\\n')\n"
                        "@M.app.command('writer-for-test', hidden=True)\n"
                        "def w():\n"
                        "    threading.Thread(target=slow).start()\n"
                        "    atexit.register(lambda: open(P, 'a').write('hook\\n'))\n"
                        "    print('x' * 200000)\n"
                        "sys.argv = ['cst', 'writer-for-test']; M.app()"],
                       capture_output=True, text=True, timeout=120, env={"CST_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and len(r.stdout) == 200001  # a long output through a pipe arrives whole
    assert out.read_text() == "thread\nhook\n"  # both finished, in the order a normal exit runs them


def test_ctrl_c_exits_as_it_did_before(tmp_path):
    import signal
    import time

    p = subprocess.Popen([sys.executable, "-c",
                          "import sys, time; from captain_search.cli import main as M\n"
                          "@M.app.command('sleep-for-test', hidden=True)\n"
                          "def s():\n    print('ready', flush=True)\n    time.sleep(60)\n"
                          "sys.argv = ['cst', 'sleep-for-test']; M.app()"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={"CST_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert p.stdout.readline().strip() == "ready"
    time.sleep(0.2)
    p.send_signal(signal.SIGINT)
    assert p.wait(timeout=30) == 130  # measured before the change too: 130, with nothing printed
