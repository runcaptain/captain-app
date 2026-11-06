"""One text-completion interface over three backends, picked in this order:

1. Anthropic API, when ANTHROPIC_API_KEY resolves (env, keychain or secrets manager).
2. OpenRouter, when OPENROUTER_API_KEY resolves.
3. The local `claude` CLI (`claude -p`), which uses the person's own Claude Code login.

Set CST_LLM=anthropic|openrouter|claude-cli to force one. CST_MODEL overrides the model.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_MODEL = "claude-sonnet-5-5"
OPENROUTER_DEFAULT = "anthropic/claude-sonnet-5.5"


class LLMUnavailable(Exception):
    pass


@dataclass
class LLM:
    backend: str
    model: str
    _key: str | None = None

    def complete(self, system: str, messages: list[dict], max_tokens: int = 4000, temperature: float = 0.0) -> str:
        if self.backend == "anthropic":
            import anthropic

            c = anthropic.Anthropic(api_key=self._key)
            # Newer models and SDKs reject `temperature`, so it is not sent.
            r = c.messages.create(model=self.model, system=system, messages=messages, max_tokens=max_tokens)
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        if self.backend == "openrouter":
            import httpx

            body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                    "messages": [{"role": "system", "content": system}, *messages]}
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, timeout=180,
                           headers={"Authorization": f"Bearer {self._key}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        if self.backend == "claude-cli":
            return _claude_cli(system, messages)
        if self.backend == "fake":
            return _FAKE.pop(0) if _FAKE else "{}"
        raise LLMUnavailable(f"unknown backend {self.backend}")


_FAKE: list[str] = []  # tests push canned replies here


def _claude_cli(system: str, messages: list[dict], timeout: float = 300) -> str:
    """`claude -p` prints its JSON result as one line, then can linger for a long time on exit
    (background work). So read that first line and stop the process ourselves."""
    import signal
    import tempfile
    import threading

    convo = "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
    proc = subprocess.Popen(
        ["claude", "-p", "--output-format", "json", "--append-system-prompt", system, "--max-turns", "1",
         "--strict-mcp-config", "--no-session-persistence",
         # A Stop hook in some setups keeps `claude -p` alive for minutes; this call needs none.
         "--settings", json.dumps({"disableAllHooks": True}),
         "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        cwd=tempfile.gettempdir(), start_new_session=True,
    )
    box: list[str] = []
    t = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    proc.stdin.write(convo + "\n\nASSISTANT:")
    proc.stdin.close()
    t.start()
    t.join(timeout)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not box or not box[0].strip():
        raise LLMUnavailable("the claude CLI returned nothing. Is Claude Code signed in? Or set ANTHROPIC_API_KEY.")
    try:
        j = json.loads(box[0])
    except json.JSONDecodeError:
        return box[0]
    if j.get("is_error"):
        raise LLMUnavailable(f"claude CLI error: {str(j.get('result'))[:300]}")
    return j.get("result", "")


def get_llm() -> LLM:
    from .workspace import get_secret

    forced = os.environ.get("CST_LLM")
    model = os.environ.get("CST_MODEL")
    if forced == "fake":
        return LLM("fake", "fake")
    if forced in (None, "anthropic"):
        try:
            return LLM("anthropic", model or DEFAULT_MODEL, get_secret("ANTHROPIC_API_KEY"))
        except KeyError:
            if forced:
                raise LLMUnavailable("ANTHROPIC_API_KEY is not set. Run `cst secret set ANTHROPIC_API_KEY`.") from None
    if forced in (None, "openrouter"):
        try:
            return LLM("openrouter", model or OPENROUTER_DEFAULT, get_secret("OPENROUTER_API_KEY"))
        except KeyError:
            if forced:
                raise LLMUnavailable("OPENROUTER_API_KEY is not set.") from None
    if shutil.which("claude"):
        return LLM("claude-cli", model or "default")
    raise LLMUnavailable(
        "no LLM available. Set ANTHROPIC_API_KEY with `cst secret set ANTHROPIC_API_KEY`, or install Claude Code."
    )


def parse_json(text: str):
    """Pull the first JSON object out of a model reply."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < 0:
        raise ValueError(f"no JSON object in reply: {text[:200]}")
    return json.loads(s[i : j + 1])
