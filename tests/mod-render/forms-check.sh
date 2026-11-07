#!/bin/bash
# CAPTAIN: a real-window check for every designed form (defaults/mods/captain-forms/tasks.js). Neither
# `captain-app check` (it draws only the New menu) nor forms-e2e.mjs (it calls submit directly) draws a form, so a
# form that can't render shipped once. This opens a throwaway "Captain (test)" window (its own home, core session
# and cst workspace), steps through every task with the preview hook (shared "captain.forms.preview", one form per
# bare `mod.open`), screenshots each at 1100x750, and fails on window errors, gpui-kit's "This view could not be
# rendered" banner, or a mostly blank form (forms-shot.py). It first proves the analyzer fails a broken and a blank
# form. The window never takes focus, and everything it starts is stopped by pid or by its own session.
#
#   tests/mod-render/forms-check.sh [captain-app binary]      default: target/debug/captain-app
set -euo pipefail
cd "$(dirname "$0")/../.."
BIN=${1:-target/debug/captain-app}
CST=${CST_BIN:-$(ls dist/Captain.app/Contents/Resources/cst/bin/cst 2>/dev/null || command -v cst)}
HOME_DIR=$(mktemp -d /tmp/cform.XXXX)
WORK=$(mktemp -d)
OUT=${SHOT_DIR:-$WORK/shots}
SESSION=test-forms-$$
mkdir -p "$OUT" "$HOME_DIR/mods"
APP_PID=""
HC=$(command -v herdr-core || echo "$HOME/.local/bin/herdr-core")
# End and remove every terminal in this window's core session (asking only while it runs: asking a stopped
# session anything starts it again). Same steps as shot-check.sh.
end_terminals() {
  for t in $(HC_SESSION=$SESSION "$HC" ls --all 2>/dev/null | python3 -c "import json,sys; print(' '.join(x['address'] for x in json.load(sys.stdin).get('terminals', [])))" 2>/dev/null); do
    HC_SESSION=$SESSION "$HC" kill "$t" >/dev/null 2>&1 || true
    sleep 0.5
    HC_SESSION=$SESSION "$HC" rm "$t" >/dev/null 2>&1 || true
  done
}
cleanup() {
  # A window that quits parks its live terminals in a holder that outlives `server stop`: end them while the
  # window still runs, quit it, stop the server, then stop any late holder (its launchd wrapper first).
  [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null && end_terminals
  [[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.5; done
  [[ -d "$HOME/.local/state/herdr-core/$SESSION" ]] && HC_SESSION=$SESSION "$HC" server stop >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    sleep 3
    pgrep -f "herdr-core hold.*/$SESSION/" >/dev/null || continue
    for h in $(pgrep -f "herdr-core hold.*/$SESSION/" || true); do
      w=$(ps -o ppid= -p "$h" | tr -d ' ')
      if [[ -n "$w" && "$w" != 1 ]] && ps -o command= -p "$w" | grep -q -- "--hc-launchd"; then kill "$w" 2>/dev/null || true; fi
      kill "$h" 2>/dev/null || true
    done
  done
  rm -rf "$HOME/.local/state/herdr-core/$SESSION"
  for p in $(pgrep -f "cst serve" || true); do
    if ps eww -p "$p" | tr ' ' '\n' | grep -qx "CST_HOME=$WORK/cst"; then kill "$p" 2>/dev/null || true; fi
  done
  rm -rf "$HOME_DIR"
}
trap cleanup EXIT

echo "seeding a scratch workspace"
CST_HOME=$WORK/cst "$CST" init forms --storage "$WORK/store" >/dev/null 2>&1
CST_HOME=$WORK/cst "$CST" demo seed >/dev/null 2>&1

# Every task, with values that bring out the fields that only appear after a choice.
PREVIEW=$(node --import ./tests/mod-render/stubs/register.mjs -e '
  const { TASKS } = await import("./defaults/mods/captain-forms/tasks.js");
  const extra = { dataset: [{ from: "sql" }], cloud: [{ provider: "aws", auth: "keys" }], compute: [{ kind: "aws-batch" }] };
  const list = [];
  for (const t of TASKS) { list.push(t.id); for (const values of extra[t.id] ?? []) list.push({ task: t.id, values }); }
  console.log(JSON.stringify(list));')
NAMES=$(echo "$PREVIEW" | python3 -c "import json,sys; print(' '.join((e if isinstance(e,str) else e['task']+'-'+'-'.join(str(v) for v in e['values'].values())) for e in json.load(sys.stdin)))")

# Two throwaway mods that must fail the analyzer: one breaks like the bug did, one draws nothing.
for kind in broken blank; do
  mkdir -p "$HOME_DIR/mods/forms-$kind"
  echo '{ "place": "none", "overlay": { "width": 560, "height": 600 } }' > "$HOME_DIR/mods/forms-$kind/mod.json"
done
cat > "$HOME_DIR/mods/forms-broken/main.js" <<'JS'
import { View, div } from "gpui-kit";
import { InputState, Input } from "gpui-base";
export default class Broken extends View { render() { return div().size_full().bg("#ffffff").child(Input.new(InputState.new({}))); } }
JS
cat > "$HOME_DIR/mods/forms-blank/main.js" <<'JS'
import { View, div } from "gpui-kit";
export default class Blank extends View { render() { return div().size_full().bg("#ffffff"); } }
JS

printf '{"appearance":"light"}\n' > "$HOME_DIR/settings.json"
printf '{"captain.forms.preview":%s,"frame.welcomed":"skipped"}\n' "$PREVIEW" > "$HOME_DIR/shared.json"
printf '{"state":"windowed","bounds":[80,80,1100,750]}\n' > "$HOME_DIR/window.json"
CAPTAIN_TEST_WINDOW=1 CAPTAIN_WINDOW_TITLE="Captain (test)" CAPTAIN_APP_HOME=$HOME_DIR CAPTAIN_CORE_SESSION=$SESSION HC_SESSION=$SESSION CST_BIN=$CST CST_HOME=$WORK/cst "$BIN" >"$WORK/app.log" 2>&1 &
APP_PID=$!
for _ in $(seq 1 60); do [[ -S $HOME_DIR/captain-app.sock ]] && break; sleep 0.5; done
[[ -S $HOME_DIR/captain-app.sock ]] || { echo "the window did not start: $(tail -5 "$WORK/app.log")"; exit 1; }
app() { CAPTAIN_APP_HOME=$HOME_DIR "$BIN" "$@"; }
sleep 6
# Anything a first start opens over the window (What's New) closes by id.
for id in $(app status | python3 -c "import json,sys; print(' '.join(o for o in json.load(sys.stdin).get('overlays', []) if o != 'toast'))"); do
  app do overlay.close "{\"id\":\"$id\"}" >/dev/null 2>&1 || true
done

# Overlays open now, except toasts.
overlays() { app status | python3 -c "import json,sys; print(' '.join(o for o in json.load(sys.stdin).get('overlays', []) if o != 'toast'))"; }
shoot() { # mod, shot name. Fails unless that mod is the overlay on screen when the shot is taken.
  for id in $(overlays); do app do overlay.close "{\"id\":\"$id\"}" >/dev/null 2>&1 || true; done
  app do mod.open "{\"name\":\"$1\"}" >/dev/null
  sleep 2.5
  app shot "$OUT/$2.png" >/dev/null
  SHOWN=$(overlays)
  app do mod.open "{\"name\":\"$1\"}" >/dev/null
  sleep 0.5
}

fail=0
for kind in broken blank; do
  shoot "forms-$kind" "selftest-$kind"
  [[ "$SHOWN" == *forms-$kind* ]] || { echo "SELF-TEST FAILED: forms-$kind wasn't on screen (overlays: $SHOWN)"; app status | python3 -c "import json,sys; print(json.load(sys.stdin).get('errors'))"; fail=1; }
  if python3 tests/mod-render/forms-shot.py "$OUT/selftest-$kind.png"; then echo "SELF-TEST FAILED: the analyzer passed a $kind form ($OUT/selftest-$kind.png)"; fail=1; else echo "self-test: a $kind form is caught"; fi
done
rm -rf "$HOME_DIR/mods/forms-broken" "$HOME_DIR/mods/forms-blank"

for n in $NAMES; do
  shoot captain-forms "$n"
  errs=$(app status | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('errors', [])))")
  if [[ "$SHOWN" != *captain-forms* ]]; then echo "$n: NOT ON SCREEN (overlays: $SHOWN) $OUT/$n.png"; app status | python3 -c "import json,sys; print(json.load(sys.stdin).get('errors'))"; fail=1; continue; fi
  if python3 tests/mod-render/forms-shot.py "$OUT/$n.png" && [[ $errs == 0 ]]; then echo "$n: drawn"; else echo "$n: NOT DRAWN ($(python3 tests/mod-render/forms-shot.py "$OUT/$n.png" --verbose | cut -c1-120); errors $errs) $OUT/$n.png"; fail=1; fi
done
[[ $fail == 0 ]] && echo "all forms drawn" || echo "shots: $OUT"
exit $fail
