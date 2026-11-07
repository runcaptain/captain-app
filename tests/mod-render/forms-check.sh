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
# This repo's backend first (its source via PYTHONPATH, even when the venv came from another checkout), then the
# bundled one, then a cst on PATH (which may be older than this tree's routes).
CST=${CST_BIN:-$(ls "$PWD/backend/core/.venv/bin/cst" 2>/dev/null || ls dist/Captain.app/Contents/Resources/cst/bin/cst 2>/dev/null || command -v cst)}
[[ "$CST" == "$PWD/backend/core/.venv/bin/cst" ]] && export PYTHONPATH="$PWD/backend/core${PYTHONPATH:+:$PYTHONPATH}"
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
  # The scratch workspace (its store and logs) goes too; shots stay only when SHOT_DIR points outside it.
  case "${WORK:-}" in "${TMPDIR:-/tmp}"*|/tmp/*|/var/folders/*) rm -rf "${WORK:?}" ;; esac
}
trap cleanup EXIT

echo "seeding a scratch workspace"
CST_HOME=$WORK/cst "$CST" init test --storage "$WORK/store" >/dev/null 2>&1
CST_HOME=$WORK/cst "$CST" demo seed >/dev/null 2>&1

# Every task, with values that bring out the fields that only appear after a choice.
PREVIEW=$(node --import ./tests/mod-render/stubs/register.mjs -e '
  const { TASKS } = await import("./defaults/mods/captain-forms/tasks.js");
  const extra = { dataset: [{ from: "sql" }], cloud: [{ provider: "aws", auth: "keys" }], compute: [{ kind: "aws-batch" }], database: [{ kind: "snowflake" }, { kind: "databricks" }] };
  const list = [];
  for (const t of TASKS.filter((t) => !t.opens)) { list.push(t.id); for (const values of extra[t.id] ?? []) list.push({ task: t.id, values }); }
  list.push("menu");
  console.log(JSON.stringify(list));')
NAMES=$(echo "$PREVIEW" | python3 -c "import json,sys; print(' '.join((('new-menu' if e == 'menu' else e) if isinstance(e,str) else e['task']+'-'+'-'.join(str(v) for v in e['values'].values())) for e in json.load(sys.stdin)))")

# Screens with charts, opened through shared "captain.open" by a throwaway mod (the next entry of "test.nav" per open).
mkdir -p "$HOME_DIR/mods/nav-shot"
echo '{ "place": "none", "overlay": { "kind": "popup", "width": 1, "height": 1 } }' > "$HOME_DIR/mods/nav-shot/mod.json"
cat > "$HOME_DIR/mods/nav-shot/main.js" <<'JS'
import { View, div } from "gpui-kit";
import { props, run, shared, setShared, closeOverlay } from "herdr";
export default class NavShot extends View {
  init(_p, cx) {
    const list = shared("test.nav") ?? [], i = Number(shared("test.nav.at")) || 0, o = list[i % Math.max(1, list.length)];
    setShared("test.nav.at", i + 1);
    if (o) { setShared("captain.open", { ...o, at: Date.now() }); run("layout.select", { stack: "sections", child: o.section }); }
    cx.timer.after(1, () => closeOverlay(props()?.overlay));
  }
  render() { return div(); }
}
JS
# The Explorer folded so its MLflow heading is on screen, and open (it reads MLflow on first open).
mkdir -p "$HOME_DIR/mods/folds-shot"
cp -R defaults/captain/kit "$HOME_DIR/mods/folds-shot/kit"
echo '{ "place": "none", "overlay": { "kind": "popup", "width": 1, "height": 1 } }' > "$HOME_DIR/mods/folds-shot/mod.json"
cat > "$HOME_DIR/mods/folds-shot/main.js" <<'JS'
import { View, div } from "gpui-kit";
import { props, shared, setShared, closeOverlay } from "herdr";
import { workspace } from "./kit/cst.js";
export default class FoldsShot extends View {
  init(_p, cx) {
    const ws = workspace() ?? shared("captain.workspace") ?? "";
    setShared("captain.explorer.folds", { ...(shared("captain.explorer.folds") ?? {}), [ws]: { "/group:brand": false, "sc-acme-outdoors": false, "all-engines": false, mlflow: true } });
    cx.timer.after(1, () => closeOverlay(props()?.overlay));
  }
  render() { return div(); }
}
JS
# Every chart the kit draws (tests/mod-render/charts-gallery), with the kit beside it as mods need.
mkdir -p "$HOME_DIR/mods/charts-gallery"
cp tests/mod-render/charts-gallery/* "$HOME_DIR/mods/charts-gallery/"
cp -R defaults/captain/kit "$HOME_DIR/mods/charts-gallery/kit"
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
printf '{"captain.forms.preview":%s,"frame.welcomed":"skipped","captain.explorer.sample":true,"captain.explain.preview":{"pipeline":"demo-bm25","q":"earbuds under 50","vs":"demo-tuned"},"test.nav":[{"section":"investigations"},{"section":"handoff"},{"section":"project"},{"section":"comparisons","backtest":"demo-tuned-vs-bm25"},{"section":"comparisons","backtest":"demo-tuned-vs-bm25","tab":"segments"},{"section":"comparisons","backtest":"demo-tuned-vs-bm25","tab":"profile","profile":true},{"section":"data","dataset":"demo-judgments","tab":"health"},{"section":"data","dataset":"demo-catalog","tab":"insights"},{"section":"data","dataset":"demo-catalog","tab":"quality"},{"section":"runs","replay":"shopping-trigger"}],"captain.sandbox.preview":[{"pipeline":"demo-tuned","knobs":{"boosts.0.weight":"1, 1.15, 1.4, 1.8"},"sample":30},{"pipeline":"demo-tuned","knobs":{"boosts.0.weight":"1, 1.15, 1.4","fields.title":"2, 3"},"sample":30}]}\n' "$PREVIEW" > "$HOME_DIR/shared.json"
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
# Close what's open. `mod.open` toggles a mod's overlay, and the status lists overlays by mod name (overlay.close
# wants an id it doesn't give), so toggle each one off until none is left.
close_all() {
  for _ in 1 2 3; do
    local open; open=$(overlays)
    [[ -z "$open" ]] && return 0
    for name in $open; do app do mod.open "{\"name\":\"$name\"}" >/dev/null 2>&1 || true; done
    sleep 0.6
  done
}
shoot() { # mod, shot name. Fails unless that mod is the overlay on screen when the shot is taken.
  close_all
  app do mod.open "{\"name\":\"$1\"}" >/dev/null
  sleep 2.5
  app shot "$OUT/$2.png" >/dev/null
  SHOWN=$(overlays)
  close_all
}

# Warm up: right after start, an overlay can open before the window is ready for it and never show. Open a
# throwaway one until it shows, so the first real check isn't the one that finds out.
for _ in $(seq 1 20); do
  app do mod.open '{"name":"forms-blank"}' >/dev/null 2>&1; sleep 1
  if [[ "$(overlays)" == *forms-blank* ]]; then app do mod.open '{"name":"forms-blank"}' >/dev/null 2>&1; sleep 0.5; break; fi
done

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
# New workspace: storage, then the drop zone (kit/setup.js). The welcome tour's first page draws the same flow, but
# the tour opens as its own window, outside this window's shot; `captain-app check defaults/frame/welcome` covers it.
shoot captain-setup setup
if [[ "$SHOWN" == *captain-setup* ]] && python3 tests/mod-render/forms-shot.py "$OUT/setup.png" --any; then echo "setup: drawn"; else echo "setup: NOT DRAWN ($SHOWN) $OUT/setup.png"; fail=1; fi
# The Explorer (its sample tree, turned on only in this window) and the scope screens.
close_all
sleep 1; app shot "$OUT/explorer.png" >/dev/null; echo "explorer: shot $OUT/explorer.png"
app do mod.open '{"name":"folds-shot"}' >/dev/null; sleep 6
app shot "$OUT/explorer-mlflow.png" >/dev/null; echo "explorer-mlflow: shot $OUT/explorer-mlflow.png"
for m in captain-scope-model captain-scope captain-rankings captain-explain; do
  shoot "$m" "$m"
  if [[ "$SHOWN" == *"$m"* ]] && python3 tests/mod-render/forms-shot.py "$OUT/$m.png" --any; then echo "$m: drawn"; else echo "$m: NOT DRAWN ($SHOWN) $OUT/$m.png"; fail=1; fi
done
# The screens with charts: shot for a person to look at (the gallery below is the automatic check).
# Investigations and Handoff with the terminal column open, their narrowest; then the palette.
for shot in investigations handoff project; do
  close_all
  app do mod.open '{"name":"nav-shot"}' >/dev/null
  sleep 8
  app shot "$OUT/$shot.png" >/dev/null && echo "$shot: shot $OUT/$shot.png"
done
close_all
app do app.palette >/dev/null; sleep 2
app shot "$OUT/palette.png" >/dev/null && echo "palette: shot $OUT/palette.png"
app do app.palette >/dev/null; sleep 1 # it toggles; close_all doesn't close it
close_all
# The terminal column hidden, so the screens get the width a person gives them (it's shown again after).
app do layout.hide '{"id":"work"}' >/dev/null
for shot in compare-overview compare-segments compare-profile data-health data-insights data-quality runs-replay; do
  close_all
  app do mod.open '{"name":"nav-shot"}' >/dev/null
  sleep "$([[ $shot == compare-profile ]] && echo 30 || echo 8)"
  app shot "$OUT/$shot.png" >/dev/null && echo "$shot: shot $OUT/$shot.png"
done
app do layout.show '{"id":"work"}' >/dev/null
app do layout.select '{"stack":"sections","child":"investigations"}' >/dev/null
# Charts: each chart type in a frame; a flat or missing chart fails, and the planted flat-line bug must be caught.
shoot charts-gallery charts
if [[ "$SHOWN" == *charts-gallery* ]] && python3 tests/mod-render/charts-shot.py "$OUT/charts.png"; then echo "charts: drawn $OUT/charts.png"; else echo "charts: NOT DRAWN ($SHOWN) $OUT/charts.png"; fail=1; fi
# What if...: the test hook runs a small sweep on each open (one knob, then two), so the line and the grid draw.
for shot in sandbox-line sandbox-grid; do
  close_all
  app do mod.open '{"name":"captain-sandbox"}' >/dev/null
  sleep 25
  app shot "$OUT/$shot.png" >/dev/null
  SHOWN=$(overlays)
  close_all
  if [[ "$SHOWN" == *captain-sandbox* ]] && python3 tests/mod-render/forms-shot.py "$OUT/$shot.png" --any; then echo "$shot: drawn $OUT/$shot.png"; else echo "$shot: NOT DRAWN ($SHOWN) $OUT/$shot.png"; fail=1; fi
done
# Closing the only terminal leaves its space open and empty, offering a new terminal; nothing starts one by itself.
close_all
spaces_before=$(app status | python3 -c "import json,sys; print(json.load(sys.stdin).get('space') or '')" 2>/dev/null)
app do tab.close '{"confirmed":true}' >/dev/null; sleep 3
app shot "$OUT/empty-space.png" >/dev/null
spaces_after=$(app status | python3 -c "import json,sys; print(json.load(sys.stdin).get('space') or '')" 2>/dev/null)
echo "empty-space: space '$spaces_before' before, '$spaces_after' after closing the last terminal; shot $OUT/empty-space.png"
[[ -n "$spaces_before" && "$spaces_before" == "$spaces_after" ]] || { echo "empty-space: NOT KEPT (the space closed with its terminal)"; fail=1; }
[[ $fail == 0 ]] && echo "all forms drawn" || echo "shots: $OUT"
exit $fail
