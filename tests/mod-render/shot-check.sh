#!/bin/bash
# CAPTAIN: a real-window check for the native section mods. The Node render tests can't see "draws nothing in
# GPUI"; this opens a throwaway Captain (its own home, core session and cst workspace, so a running Captain is never
# touched), installs each mod from defaults/mods as a personal mod, shows its section, takes shots until the content
# is drawn, and fails if the section stays blank. Everything it starts is stopped by pid.
#
#   tests/mod-render/shot-check.sh [captain-app binary] [mod ...]
#   defaults: target/release/captain-app; captain-data captain-runs captain-compare (those present)
set -euo pipefail
cd "$(dirname "$0")/../.."
BIN=${1:-target/release/captain-app}
shift || true
MODS=("$@")
if [[ ${#MODS[@]} -eq 0 ]]; then
  for m in captain-data captain-runs captain-compare; do [[ -d defaults/mods/$m ]] && MODS+=("$m"); done
fi
block_of() { case "$1" in captain-project) echo project ;; captain-data) echo data ;; captain-runs) echo runs ;; captain-compare) echo comparisons ;; captain-investigations) echo investigations ;; *) echo "" ;; esac; }
# Extra views after the plain ones (the two that open the import wizard last, so it covers nothing after them): a query side by side (props the mods read at init).
EXTRA=()
for m in "${MODS[@]}"; do
  case "$m" in
    captain-data) EXTRA+=("captain-data:notebook" "captain-data:chart" "captain-data:bignotebook" "captain-data:sqltools" "captain-data:sample" "captain-data:samplefiles" "captain-data:samplerow" "captain-data:pictures" "captain-data:tablerows" "captain-data:wide40" "captain-data:wide13" "captain-data:models" "captain-data:tables" "captain-data:lineage" "captain-data:insights" "captain-data:quality" "captain-data:import" "captain-data:import-table") ;;
    # needs SHOT_PROJECT=1: a project with pins and experiments. captain-project:list (the project cards, nothing picked):
    # SHOT_ONLY, as two cards leave the column the blank check measures nearly empty.
    captain-project) EXTRA+=("captain-project:home") ;;
    captain-runs) EXTRA+=("captain-runs:jobs" "captain-runs:live") ;;  # captain-runs:grading: SHOT_ONLY (one card reads as too empty for the check)
    captain-compare) EXTRA+=("captain-compare:side" "captain-compare:imported" "captain-compare:imported-queries" "captain-compare:profile") ;;
  esac
done

# A short home: the window's control socket must fit in a Unix socket path.
HOME_DIR=$(mktemp -d /tmp/cshot.XXXX)
SESSION=test-shot-$$
echo "core session $SESSION"
WORK=$(mktemp -d)
OUT=${SHOT_DIR:-$WORK/shots}
mkdir -p "$OUT"
APP_PID=""
HC=$(command -v herdr-core || echo "$HOME/.local/bin/herdr-core")
# End and remove every terminal in this window's core session (asking only while it runs: asking a stopped session
# anything starts it again).
end_terminals() {
  for t in $(HC_SESSION=$SESSION "$HC" ls --all 2>/dev/null | python3 -c "import json,sys; print(' '.join(x['address'] for x in json.load(sys.stdin).get('terminals', [])))" 2>/dev/null); do
    HC_SESSION=$SESSION "$HC" kill "$t" >/dev/null 2>&1 || true
    sleep 0.5
    HC_SESSION=$SESSION "$HC" rm "$t" >/dev/null 2>&1 || true
  done
}
cleanup() {
  # The window's terminals run in their own core session, under a server that outlives the window, and a window
  # that quits parks its live terminals in a holder that stays. So end them while the window still runs, then quit
  # it, then stop the server, and keep looking for a late holder for 15 s.
  [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null && end_terminals
  [[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.5; done
  [[ -d "$HOME/.local/state/herdr-core/$SESSION" ]] && HC_SESSION=$SESSION "$HC" server stop >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7; do
    sleep 3
    if pgrep -f "herdr-core hold.*/$SESSION/" >/dev/null; then
      end_terminals
      HC_SESSION=$SESSION "$HC" server stop >/dev/null 2>&1 || true
      # Still there: stop its launchd wrapper first (else it starts a new holder), then the holder, by pid.
      for h in $(pgrep -f "herdr-core hold.*/$SESSION/" || true); do
        w=$(ps -o ppid= -p "$h" | tr -d ' ')
        if [[ -n "$w" && "$w" != 1 ]] && ps -o command= -p "$w" | grep -q -- "--hc-launchd"; then kill "$w" 2>/dev/null || true; fi
        kill "$h" 2>/dev/null || true
      done
    fi
  done
  rm -rf "$HOME/.local/state/herdr-core/$SESSION"
  # The window starts its own cst serve on our workspace: stop that one, by pid, and nothing else.
  for p in $(pgrep -f "cst serve" || true); do
    if ps eww -p "$p" | tr ' ' '\n' | grep -qx "CST_HOME=${CSTH:-$WORK/cst}"; then kill "$p" 2>/dev/null || true; fi
  done
  # The window's shared state, kept with the shots for debugging (no secrets: the backend token is not in it).
  cp "$HOME_DIR/shared.json" "$OUT/shared.json" 2>/dev/null || true
  cp "$WORK/app.log" "$OUT/app.log" 2>/dev/null || true
  rm -rf "$HOME_DIR"
}
trap cleanup EXIT

echo "seeding a scratch workspace"
# The backend the seeding and the window use: $CST_BIN (this repo's backend/core, say), else cst on PATH.
CSTCMD=${CST_BIN:-cst}
# SHOT_CST_HOME: shoot against an existing backend home (a copy of someone's workspace) instead of a seeded one.
if [[ -n "${SHOT_CST_HOME:-}" ]]; then
  CSTH=$SHOT_CST_HOME
else
CSTH=$WORK/cst
CST_HOME=$WORK/cst "$CSTCMD" init test --storage "$WORK/store" >/dev/null 2>&1
CST_HOME=$WORK/cst "$CSTCMD" demo seed --legacy >/dev/null 2>&1
# An imported run (a readout only, no per-query rankings), as `cst demos import` makes them.
CST_PY=$(head -1 "$(command -v "$CSTCMD")" | sed 's/^#!//')
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-imported.py >/dev/null
# A notebook: text, a SQL cell on the demo store, a Python cell and a chart of the SQL result.
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-notebook.py >/dev/null
# Wide tables for the Rows tab: usage (40 columns), spend (13).
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-wide.py >/dev/null
# A dataset of three files, for the Sample data file picker.
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-multifile.py >/dev/null
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-images.py >/dev/null
# An MLflow model version linked to demo-tuned, so lineage draws MLflow upstream of it. The tracking server isn't
# there: lineage draws from the link alone.
CST_HOME=$WORK/cst "$CSTCMD" mlflow use --uri http://127.0.0.1:9 >/dev/null 2>&1 || true
CST_HOME=$WORK/cst "$CSTCMD" mlflow link ranker 3 demo-tuned >/dev/null 2>&1 || true
# A backtest run from the CLI, so Live has a finished run to draw (stages, both arms, shoppers, lines).
[[ " ${MODS[*]} " == *" captain-runs "* ]] && { env -u CLAUDECODE CST_HOME=$WORK/cst "$CSTCMD" backtest run demo-tuned-vs-bm25 >/dev/null 2>&1 || true; }
# hello-world from another query, so the import view shows the name clash.
CST_HOME=$WORK/cst "$CSTCMD" sql demo-store -e "select 1 as a" --save hello-world >/dev/null 2>&1 || true
# The demo store's tables, read, and one finished local job, for the Tables and Jobs views.
CST_HOME=$WORK/cst "$CSTCMD" tables pull demo-store >/dev/null 2>&1 || true
CST_HOME=$WORK/cst "$CSTCMD" job run local -- data list >/dev/null 2>&1 || true
fi

# Skip the first-start welcome (it opens over everything while shared "frame.welcomed" is unset).
echo '{"frame.welcomed":"skipped"}' > "$HOME_DIR/shared.json"
# SHOT_SCOPE=1: a brand with demo-catalog linked, picked in the Explorer from the start, so every list is filtered.
if [[ -n "${SHOT_SCOPE:-}" && -z "${SHOT_CST_HOME:-}" ]]; then
  SID=$(CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-scope.py | tail -1)
  printf '{"frame.welcomed":"skipped","captain.scope":{"kind":"scope","id":"%s","label":"Shot Brand"}}\n' "$SID" > "$HOME_DIR/shared.json"
  echo "Explorer pick: $SID"
fi
# SHOT_PROJECT=1: a project with registry models, picked in the Explorer from the start (for captain-project views).
if [[ -n "${SHOT_PROJECT:-}" && -z "${SHOT_CST_HOME:-}" ]]; then
  PID=$(CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-project.py | tail -1)
  printf '{"frame.welcomed":"skipped","captain.scope":{"kind":"project","id":"%s","label":"Shot Project"}}\n' "$PID" > "$HOME_DIR/shared.json"
  echo "Explorer pick: $PID"
fi
# SHOT_FORMS_PREVIEW=cloud: the audit's forms dialog opens on that form (captain-forms' test hook), e.g. to compare the
# cloud marks before and after a change. A comma list (database,cloud) shoots one dialog per form:
# audit-dialog-captain-forms-database.png, audit-dialog-captain-forms-cloud.png.
if [[ -n "${SHOT_FORMS_PREVIEW:-}" ]]; then
  python3 - "$HOME_DIR/shared.json" "$SHOT_FORMS_PREVIEW" <<'PY'
import json, sys
p, form = sys.argv[1:]
d = json.load(open(p))
d["captain.forms.preview"] = form.split(",") if "," in form else form
json.dump(d, open(p, "w"))
PY
fi
# SHOT_WINDOW=1100x750: the window at that size (logical pixels). SHOT_WINDOW=max: maximized after it starts.
if [[ "${SHOT_WINDOW:-}" =~ ^([0-9]+)x([0-9]+)$ ]]; then
  printf '{"state":"windowed","bounds":[80.0,60.0,%s.0,%s.0]}\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" > "$HOME_DIR/window.json"
fi
# SHOT_AUDIT: a helper mod that opens the Explorer's MLflow heading while the window runs (mods are found at launch).
if [[ -n "${SHOT_AUDIT:-}" ]]; then
  mkdir -p "$HOME_DIR/mods/folds-open"; cp -R defaults/captain/kit "$HOME_DIR/mods/folds-open/kit"
  echo '{ "place": "none", "overlay": { "kind": "popup", "width": 1, "height": 1 } }' > "$HOME_DIR/mods/folds-open/mod.json"
  cat > "$HOME_DIR/mods/folds-open/main.js" <<'JS'
import { View, div } from "gpui-kit";
import { props, shared, setShared, closeOverlay } from "herdr";
import { workspace } from "./kit/cst.js";
export default class FoldsOpen extends View {
  init(_p, cx) {
    const ws = workspace() ?? shared("captain.workspace") ?? "";
    setShared("captain.explorer.folds", { ...(shared("captain.explorer.folds") ?? {}), [ws]: { "all-engines": false, mlflow: true } });
    cx.timer.after(1, () => closeOverlay(props()?.overlay));
  }
  render() { return div(); }
}
JS
fi
launch() {
  # "Captain (test)" in the title bar, so nobody mistakes this window for their own Captain.
  CAPTAIN_TEST_WINDOW=1 CAPTAIN_WINDOW_TITLE="Captain (test)" CAPTAIN_APP_HOME=$HOME_DIR CAPTAIN_CORE_SESSION=$SESSION HC_SESSION=$SESSION CST_HOME=$CSTH "$BIN" >"$WORK/app.log" 2>&1 &
  APP_PID=$!
  for _ in $(seq 1 60); do [[ -S $HOME_DIR/captain-app.sock ]] && break; sleep 0.5; done
  [[ -S $HOME_DIR/captain-app.sock ]] || { echo "the window did not start: $(tail -5 "$WORK/app.log")"; exit 1; }
}
launch
# SHOT_SAMPLE=1: when the window stops answering, save what its threads were doing (macOS `sample`) with the shots.
app() {
  CAPTAIN_APP_HOME=$HOME_DIR "$BIN" "$@" && return 0
  local rc=$?
  [[ -n "${SHOT_SAMPLE:-}" && -n "$APP_PID" ]] && sample "$APP_PID" 3 -file "$OUT/hang-sample.txt" >/dev/null 2>&1 || true
  return $rc
}
# cst, asking this window (CAPTAIN_APP_HOME) about this workspace (CST_HOME), as a terminal it opens would.
cst_app() { CAPTAIN_APP_HOME=$HOME_DIR CST_HOME=$CSTH "$CSTCMD" "$@"; }
# A new home's first start opens What's New (or the welcome) over everything, and those close only by id.
# The second start in the same home opens nothing, so restart the window once if anything covers it.
covered() { app status | python3 -c "import json,sys; o=json.load(sys.stdin).get('overlays', []); sys.exit(0 if any(x != 'toast' for x in o) else 1)"; }
sleep 3
if covered; then
  kill "$APP_PID" 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.5; done
  rm -f "$HOME_DIR/captain-app.sock"
  launch
  sleep 3
fi
covered && { echo "an overlay still covers the window: $(app status | python3 -c "import json,sys; print(json.load(sys.stdin).get('overlays'))")"; exit 1; }
[[ "${SHOT_WINDOW:-}" == max ]] && { app do window.maximize >/dev/null; sleep 1; }

# Props for a view: the scratch workspace's first query of the seeded replay, read from its own serve.
props_of() {
  case "$1" in
    captain-compare:side) python3 tests/mod-render/first-query.py "$CSTH" ;;
    captain-data:notebook) echo '{"tab": "notebooks", "notebook": "shot-notebook", "runAll": true}' ;;
    captain-data:models) echo '{"tab": "models"}' ;;
    captain-data:samplerow) echo '{"pageTab":"sample","dataset":"shot-multifile","openRow":2}' ;;
    captain-data:pictures) echo '{"pageTab":"sample","dataset":"shot-pictures","openRow":1}' ;;
    captain-project:choose) echo '{"chooseModels": true}' ;;
    captain-project:home) echo '{"openExperiment": true}' ;;
    captain-project:list) echo '{"allProjects": true}' ;;
    captain-data:models-attach) echo '{"tab": "models", "registryAttach": true}' ;;
    captain-data:tables) echo '{"tab": "tables"}' ;;
    captain-data:import-run) echo '{"importWith": {"conn": "demo-store", "table": "products", "name": "hello", "autoImport": true}}' ;;
    captain-data:quality) echo '{"pageTab": "quality"}' ;;
    captain-data:insights) echo '{"pageTab": "insights"}' ;;
    captain-data:lineage) echo '{"pageTab": "lineage"}' ;;
    captain-data:labels) echo '{"tab": "labels"}' ;;
    captain-data:search) echo '{"find": "price"}' ;;
    captain-data:flat) echo '{"flatAll": true}' ;;
    captain-runs:grading) echo '{"tab": "grading"}' ;;
    captain-runs:jobs) echo '{"tab": "jobs"}' ;;
    captain-runs:live) echo '{"tab": "live"}' ;;
    captain-data:chart) echo '{"tab": "notebooks", "notebook": "shot-chart", "runAll": true}' ;;
    captain-data:import) echo '{"importWith": {"conn": "demo-store", "sql": "select * from products limit 1000", "name": "Hello World"}}' ;;
    captain-data:import-table) echo '{"importWith": {"conn": "demo-store", "table": "products", "name": "Products"}}' ;;
    captain-data:sample) echo '{"pageTab": "sample"}' ;;
    captain-data:samplefiles) echo '{"pageTab": "sample", "dataset": "shot-multifile"}' ;;
    captain-data:wide40) echo '{"tab": "tables", "tablesTab": "rows", "tablesPick": "usage"}' ;;
    captain-data:wide13) echo '{"tab": "tables", "tablesTab": "rows", "tablesPick": "spend"}' ;;
    captain-data:tablerows) echo '{"tab": "tables", "tablesTab": "rows"}' ;;
    captain-data:bignotebook) echo '{"tab": "notebooks", "notebook": "shot-big", "runAll": true}' ;;
    captain-data:sqltools) echo '{"tab": "notebooks", "notebook": "shot-sql-tools", "sqlTools": {"panel": "filled"}}' ;;
    captain-compare:profile) echo '{"backtest": "demo-tuned-vs-bm25", "version": 1, "tab": "profile", "autoProfile": true}' ;;
    captain-compare:imported) echo '{"backtest": "demos/shot/imported-readout", "version": 1}' ;;
    captain-compare:imported-queries) echo '{"backtest": "demos/shot/imported-readout", "version": 1, "tab": "queries"}' ;;
    *) echo '{}' ;;
  esac
}

fail=0
VIEWS=("${MODS[@]}" ${EXTRA[@]+"${EXTRA[@]}"})
# SHOT_ONLY="view view": just those views.
[[ -n "${SHOT_ONLY:-}" ]] && read -r -a VIEWS <<< "$SHOT_ONLY"
for view in "${VIEWS[@]}"; do
  m=${view%%:*}
  props=$(props_of "$view")
  block=$(block_of "$m")
  [[ -n "$block" ]] || { echo "$m: no section block known"; fail=1; continue; }
  rm -rf "$HOME_DIR/mods/$m"
  mkdir -p "$HOME_DIR/mods" "$HOME_DIR/layouts"
  cp -R "defaults/mods/$m" "$HOME_DIR/mods/$m"
  # The import wizard opens over Data and signals.
  [[ "$view" == captain-data:import* ]] && { rm -rf "$HOME_DIR/mods/captain-import"; cp -R defaults/mods/captain-import "$HOME_DIR/mods/captain-import"; }
  rm -f "$HOME_DIR/mods/$m/gpui-kit.d.ts" "$HOME_DIR/mods/$m/jsconfig.json"
  # Captain's own layout can't be edited (edits land in a copy), so each view gets its own personal layout,
  # a copy of the shipped one with this block set; switching to a new layout builds its mods fresh.
  n_view=$((${n_view:-0} + 1))
  python3 - defaults/layouts/captain.json "$HOME_DIR/layouts/shot$n_view.json" "$block" "$m" "$props" <<'EOF'
import json, sys
src, p, block, mod, props = sys.argv[1:]
d = json.load(open(src))
def find(n):
    if n.get("id") == block: return n
    for c in n.get("children", []):
        r = find(c)
        if r: return r
n = find(d)
n["content"] = {"mod": mod, "props": json.loads(props)} if json.loads(props) else {"mod": mod}
json.dump(d, open(p, "w"), indent=2)
EOF
  sleep 2
  app do layout.switch "{\"name\":\"shot$n_view\"}" >/dev/null
  sleep 1
  app do layout.select "{\"stack\":\"sections\",\"child\":\"$block\"}" >/dev/null
  ok=0
  # The notebook view runs every cell on a fresh Python first: give it time to finish before judging.
  [[ "$view" == captain-data:notebook || "$view" == captain-data:chart || "$view" == captain-data:bignotebook ]] && sleep 25
  [[ "$view" == captain-data:import* || "$view" == captain-compare:profile || "$view" == captain-data:quality ]] && sleep 8
  [[ "$view" == captain-data:import-run ]] && sleep 14
  [[ "$view" == captain-data:models-attach ]] && sleep 25  # it lists every bucket of the login first
  [[ "$view" == captain-data:pictures ]] && sleep 20  # the rows, then their thumbnails
  for i in $(seq 1 12); do
    sleep 1.5
    # A window in the background may not draw a new frame after its data arrives, and shot saves the last frame
    # drawn: ask for the same section again, which draws one.
    app do layout.select "{\"stack\":\"sections\",\"child\":\"$block\"}" >/dev/null || true
    sleep 0.5
    app shot "$OUT/${view/:/-}.png" >/dev/null
    # A frozen window keeps answering with its last frame: a shot identical to the previous view's isn't this view.
    sum=$(shasum "$OUT/${view/:/-}.png" | cut -d' ' -f1)
    if [[ "$sum" == "${PREV_SUM:-}" ]]; then echo "$view: same frame as the previous view, waiting for a redraw"; continue; fi
    if python3 tests/mod-render/shot-busy.py "$OUT/${view/:/-}.png"; then ok=1; break; fi
  done
  PREV_SUM=$(shasum "$OUT/${view/:/-}.png" | cut -d' ' -f1)
  errs=$(app status | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('errors', [])))")
  if [[ $ok == 1 && $errs == 0 ]]; then echo "$view: drawn ($OUT/${view/:/-}.png)"; else echo "$view: BLANK or errors ($errs) ($OUT/${view/:/-}.png)"; app status; fail=1; fi
  # What an agent sees of this view: `cst app context` asks this window and must name the section it shows.
  # (A dialog over the page, the import wizard, leaves the section under it as it was.) SHOT_CONTEXT=1 prints it.
  if ctx=$(cst_app --json app context 2>&1); then
    sec=$(printf '%s' "$ctx" | python3 -c "import json,sys; print(json.load(sys.stdin).get('section'))" 2>/dev/null || echo "?")
    if [[ "$sec" != "$block" ]]; then echo "$view: cst app context says section '$sec', the window shows '$block'"; fail=1; fi
    if [[ -n "${SHOT_CONTEXT:-}" ]]; then echo "--- cst app context ($view)"; cst_app app context || true; echo "---"; fi
  else
    echo "$view: cst app context failed: $ctx"; fail=1
  fi
done

# `cst app shot`: a real picture of this window, saved under the window's own cache folder and nowhere else.
if pic=$(cst_app app shot 2>&1) && [[ "$pic" == "$HOME_DIR/cache/shots/"*.png && -s "$pic" ]] && python3 tests/mod-render/shot-busy.py "$pic"; then
  cp "$pic" "$OUT/cst-app-shot.png"; echo "cst app shot: a picture of the window ($pic, kept as $OUT/cst-app-shot.png)"
else
  echo "cst app shot: no picture of the window: $pic"; fail=1
fi
# `cst app open`: an agent shows the person a screen, a record or a filled form, and the window follows. Read back
# with `cst app context`, in Captain's own layout (every shipped mod), with whatever a view left open closed first.
for _ in 1 2 3; do app do overlay.close '{}' >/dev/null 2>&1 || true; done
app do layout.switch '{"name":"captain"}' >/dev/null || true
sleep 3
now() { cst_app --json app context --bare 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin); g=d.get('dialog') or {}
print(d.get('section'), (d.get('selected') or {}).get('dataset') or (d.get('selected') or {}).get('backtest') or '-', g.get('form') or '-', (g.get('fields') or {}).get('title') or '-', sep='|')" 2>/dev/null || echo "?"; }
opened() { # opened <what was asked> <pattern the context must match>
  local got=""
  for _ in $(seq 1 12); do sleep 1; got=$(now); [[ "$got" == $2 ]] && { echo "cst app open $1: the window shows it ($got)"; return 0; }; done
  echo "cst app open $1: the window shows $got, expected $2"; fail=1
}
cst_app app open runs >/dev/null && opened runs 'runs|*'
cst_app app open dataset:demo-queries >/dev/null && opened dataset:demo-queries 'data|demo-queries|*'
cst_app app open compare >/dev/null && opened compare 'comparisons|*'
cst_app app open form:investigation --value "title=Opened by an agent" >/dev/null && opened form:investigation '*|investigation|Opened by an agent'
app shot "$OUT/cst-app-open-form.png" >/dev/null || true
app do overlay.close '{}' >/dev/null 2>&1 || true
# Pinned things: `cst context add` puts them in this window's sidebar strip, and `cst context show` expands them.
if cst_app context add demo-catalog run:demo-tuned-vs-bm25 >/dev/null 2>&1 && cst_app --json context show | python3 -c "
import json,sys
d=json.load(sys.stdin); p={i['kind']: i.get('summary') or {} for i in d['pinned']}
assert d['captain_running'] and d['view']['section'], d
assert p['dataset'].get('rows') and len(p['dataset'].get('sample') or []) == 3, p
assert p['run'].get('status'), p"; then
  sleep 4; app shot "$OUT/cst-context-strip.png" >/dev/null || true
  echo "cst context: two things pinned and expanded ($OUT/cst-context-strip.png shows the sidebar strip)"
  if [[ -n "${SHOT_CONTEXT:-}" ]]; then echo "--- cst context show"; cst_app context show | cut -c1-220 || true; echo "---"; fi
  cst_app context clear >/dev/null || true
else
  echo "cst context: pinning or expanding failed: $(cst_app context show 2>&1 | tail -3)"; fail=1
fi
# SHOT_CONTEXT=1: the same from a terminal this window opens, with nothing set by hand: Captain's terminals carry
# CAPTAIN_APP_HOME and CST_BIN (src/cst.rs terminal_env), so an agent there asks this window and no other.
if [[ -n "${SHOT_CONTEXT:-}" ]]; then
  rm -f "$OUT/from-terminal.txt"
  app do tab.new "{\"name\":\"context\",\"argv\":[\"/bin/sh\",\"-c\",\"{ echo home=\$CAPTAIN_APP_HOME; \\\"\$CST_BIN\\\" app context --bare; } > '$OUT/from-terminal.txt' 2>&1; sleep 2\"]}" >/dev/null || true
  for _ in $(seq 1 30); do grep -q "For a picture" "$OUT/from-terminal.txt" 2>/dev/null && break; sleep 1; done
  if grep -q "^home=$HOME_DIR\$" "$OUT/from-terminal.txt" 2>/dev/null && grep -q "What the person is looking at" "$OUT/from-terminal.txt"; then
    echo "--- cst app context, run inside a terminal this window opened"; cat "$OUT/from-terminal.txt"; echo "---"
  else
    echo "a terminal this window opened could not run cst app context: $(cat "$OUT/from-terminal.txt" 2>/dev/null)"; fail=1
  fi
fi

# SHOT_AUDIT=1: the whole app as a person meets it, in Captain's own layout with every current Captain mod installed:
# each section, then the palette, Settings and the Captain dialogs. Shots only (audit-*.png): a person looks at them.
if [[ -n "${SHOT_AUDIT:-}" ]]; then
  mkdir -p "$HOME_DIR/mods"
  for d in defaults/mods/captain-*; do
    m=$(basename "$d"); rm -rf "$HOME_DIR/mods/$m"; cp -R "$d" "$HOME_DIR/mods/$m"
    rm -f "$HOME_DIR/mods/$m/gpui-kit.d.ts" "$HOME_DIR/mods/$m/jsconfig.json"
  done
  # Close whatever an earlier view opened (the import wizard), so nothing covers the walk.
  for _ in 1 2 3; do app do overlay.close '{}' >/dev/null 2>&1 || true; done
  app do layout.switch '{"name":"captain"}' >/dev/null || true
  sleep 3
  shoot() { sleep "${2:-4}"; app shot "$OUT/audit-$1.png" >/dev/null && echo "audit: $1"; }
  for child in investigations data runs comparisons handoff engine; do
    app do layout.select "{\"stack\":\"sections\",\"child\":\"$child\"}" >/dev/null || true
    shoot "$child" 5
  done
  app do layout.select '{"stack":"sections","child":"data"}' >/dev/null || true
  app do app.palette >/dev/null || true; shoot palette 2; app do app.palette >/dev/null || true
  # app.settings opens Settings and never closes it (a second press doesn't stack another), so close it by id.
  app do app.settings >/dev/null || true; shoot settings 3; app do overlay.close '{"id":"settings"}' >/dev/null || true; sleep 1
  # Settings, Project Structure (the scope model's page).
  app do app.settings '{"section":"structure"}' >/dev/null || true; shoot settings-structure 5; app do overlay.close '{"id":"settings"}' >/dev/null || true; sleep 1
  app do mod.open '{"name":"folds-open"}' >/dev/null || true; shoot explorer-mlflow 6
  for m in captain-sandbox captain-explain captain-import captain-forms captain-rankings captain-setup captain-services captain-scope-model captain-cloud captain-project-icon; do
    if [[ "$m" == captain-forms && "${SHOT_FORMS_PREVIEW:-}" == *,* ]]; then
      # One dialog per form in the list: each open takes the next one.
      for form in ${SHOT_FORMS_PREVIEW//,/ }; do
        app do mod.open "{\"name\":\"$m\"}" >/dev/null || true; shoot "dialog-$m-$form" 4
        app do mod.open "{\"name\":\"$m\"}" >/dev/null || true; sleep 1
      done
      continue
    fi
    app do mod.open "{\"name\":\"$m\"}" >/dev/null || true; shoot "dialog-$m" 4
    app do mod.open "{\"name\":\"$m\"}" >/dev/null || true; sleep 1
  done
fi
# `terminal.paste {text}` ("Send to agent" uses it): the text lands in the focused terminal's input, Enter is not
# pressed, and the clipboard is exactly what it was. A program that reads 11 raw bytes stands in for the agent.
if [[ -n "${SHOT_CONTEXT:-}" ]]; then
  clip_before=$(pbpaste 2>/dev/null | shasum | cut -d' ' -f1 || true)
  rm -f "$OUT/pasted.txt"
  app do tab.new "{\"name\":\"paste\",\"argv\":[\"/bin/sh\",\"-c\",\"stty -icanon -echo; dd bs=1 count=11 of='$OUT/pasted.txt' 2>/dev/null; sleep 1\"]}" >/dev/null || true
  sleep 4
  app do terminal.paste '{"text":"hello agent"}' >/dev/null || true
  for _ in $(seq 1 15); do [[ "$(cat "$OUT/pasted.txt" 2>/dev/null)" == "hello agent" ]] && break; sleep 1; done
  clip_after=$(pbpaste 2>/dev/null | shasum | cut -d' ' -f1 || true)
  if [[ "$(cat "$OUT/pasted.txt" 2>/dev/null)" == "hello agent" && "$clip_before" == "$clip_after" ]]; then
    echo "terminal.paste {text}: the terminal got the text, and the clipboard is unchanged"
  else
    echo "terminal.paste {text}: got '$(cat "$OUT/pasted.txt" 2>/dev/null)', clipboard $([[ "$clip_before" == "$clip_after" ]] && echo unchanged || echo CHANGED)"; fail=1
  fi
fi
exit $fail
