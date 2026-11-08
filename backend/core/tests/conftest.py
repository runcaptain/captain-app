import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CST_USER", "tester@captain.dev")
    monkeypatch.delenv("CST_WORKSPACE", raising=False)
    # Tests run inside coding agents too: their markers would make every session an agent's.
    for k in ("CST_AGENT", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID", "CODEX_SANDBOX_NETWORK_DISABLED", "CURSOR_AGENT"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path
