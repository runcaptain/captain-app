"""The app says "deploy" and "deployment", never "stage" or "staging", to a person. This finds sentences in the
services' own text (string literals with a space in them, docstrings left out) that still use the old words.

It fails only for the files of the evaluation and data screens, which have none. For everything else it is a
finder: `uv run python tests/test_words_stage.py` prints every sentence it finds, with its file and line, so the
owner of that file can decide. "Stage" also has honest uses (a stage of a pipeline, a stage of a run), which is why
the rest is reported and not failed."""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "captain_search"
WORD = re.compile(r"\b(stag(e|ed|es|ing)|unstag\w*)\b", re.I)
# Files whose every sentence a person reads on the evaluation and data screens.
STRICT = ("eval/evalform.py", "eval/metrics.py", "testsets.py", "compare.py", "segments.py", "data/catalog.py")


def find() -> list[tuple[str, int, str]]:
    out = []
    for f in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and " " in n.value.strip() and WORD.search(n.value):
                out.append((str(f.relative_to(ROOT)), n.lineno, " ".join(n.value.split())[:140]))
    return out


def test_the_evaluation_and_data_text_never_says_stage():
    mine = [x for x in find() if x[0] in STRICT]
    assert mine == [], mine


def test_the_finder_sees_a_sentence_and_skips_a_docstring(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text('"""Staging things."""\ndef f():\n    """Stage it."""\n    return "Nothing is staged in any project."\nKEY = "staging"\n')
    import sys

    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert find() == [("a.py", 4, "Nothing is staged in any project.")], "one sentence: not the docstrings, not the bare key"


if __name__ == "__main__":
    rows = find()
    for f, line, text in rows:
        print(f"{f}:{line}: {text}")
    print(f"{len(rows)} sentences in {len({r[0] for r in rows})} files")
