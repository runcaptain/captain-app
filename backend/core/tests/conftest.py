import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CST_USER", "tester@captain.dev")
    monkeypatch.delenv("CST_WORKSPACE", raising=False)
    return tmp_path
