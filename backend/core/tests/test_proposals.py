

def test_agent_recognised_from_its_shell(monkeypatch):
    from captain_search import proposals as P

    monkeypatch.setattr(P, "DETECT_AGENT", True)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert P.agent_name() == "claude"
    monkeypatch.setenv("CST_AGENT", "my-bot")
    assert P.agent_name() == "my-bot"  # an explicit name wins
    monkeypatch.delenv("CST_AGENT")
    monkeypatch.setattr(P, "DETECT_AGENT", False)  # what the server does: the app's requests are a person's
    assert P.agent_name() is None
    monkeypatch.setattr(P, "DETECT_AGENT", True)
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    assert P.agent_name() == "codex"
