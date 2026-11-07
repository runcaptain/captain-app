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
block_of() { case "$1" in captain-data) echo data ;; captain-runs) echo runs ;; captain-compare) echo comparisons ;; *) echo "" ;; esac; }
# Extra views after the plain ones: a query side by side (props the mods read at init).
EXTRA=()
for m in "${MODS[@]}"; do
  case "$m" in
    captain-data) EXTRA+=("captain-data:notebook" "captain-data:chart" "captain-data:sqltools" "captain-data:import" "captain-data:import-table" "captain-data:sample" "captain-data:tablerows" "captain-data:wide40" "captain-data:wide13" "captain-data:models" "captain-data:tables" "captain-data:lineage") ;;
    captain-runs) EXTRA+=("captain-runs:jobs") ;;
    captain-compare) EXTRA+=("captain-compare:side" "captain-compare:imported" "captain-compare:imported-queries") ;;
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
CST_HOME=$WORK/cst "$CSTCMD" demo seed >/dev/null 2>&1
# An imported run (a readout only, no per-query rankings), as `cst demos import` makes them.
CST_PY=$(head -1 "$(command -v "$CSTCMD")" | sed 's/^#!//')
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-imported.py >/dev/null
# A notebook: text, a SQL cell on the demo store, a Python cell and a chart of the SQL result.
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-notebook.py >/dev/null
# Wide tables for the Rows tab: usage (40 columns), spend (13).
CST_HOME=$WORK/cst "$CST_PY" tests/mod-render/seed-wide.py >/dev/null
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
# SHOT_WINDOW=1100x750: the window at that size (logical pixels). SHOT_WINDOW=max: maximized after it starts.
if [[ "${SHOT_WINDOW:-}" =~ ^([0-9]+)x([0-9]+)$ ]]; then
  printf '{"state":"windowed","bounds":[80.0,60.0,%s.0,%s.0]}\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" > "$HOME_DIR/window.json"
fi
launch() {
  # "Captain (test)" in the title bar, so nobody mistakes this window for their own Captain.
  CAPTAIN_TEST_WINDOW=1 CAPTAIN_WINDOW_TITLE="Captain (test)" CAPTAIN_APP_HOME=$HOME_DIR CAPTAIN_CORE_SESSION=$SESSION HC_SESSION=$SESSION CST_HOME=$CSTH "$BIN" >"$WORK/app.log" 2>&1 &
  APP_PID=$!
  for _ in $(seq 1 60); do [[ -S $HOME_DIR/captain-app.sock ]] && break; sleep 0.5; done
  [[ -S $HOME_DIR/captain-app.sock ]] || { echo "the window did not start: $(tail -5 "$WORK/app.log")"; exit 1; }
}
launch
app() { CAPTAIN_APP_HOME=$HOME_DIR "$BIN" "$@"; }
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
    captain-data:tables) echo '{"tab": "tables"}' ;;
    captain-data:lineage) echo '{"pageTab": "lineage"}' ;;
    captain-runs:jobs) echo '{"tab": "jobs"}' ;;
    captain-data:chart) echo '{"tab": "notebooks", "notebook": "shot-chart", "runAll": true}' ;;
    captain-data:import) echo '{"importWith": {"conn": "demo-store", "sql": "select * from products limit 1000", "name": "Hello World"}}' ;;
    captain-data:import-table) echo '{"importWith": {"conn": "demo-store", "table": "products", "name": "Products"}}' ;;
    captain-data:sample) echo '{"pageTab": "sample"}' ;;
    captain-data:wide40) echo '{"tab": "tables", "tablesTab": "rows", "tablesPick": "usage"}' ;;
    captain-data:wide13) echo '{"tab": "tables", "tablesTab": "rows", "tablesPick": "spend"}' ;;
    captain-data:tablerows) echo '{"tab": "tables", "tablesTab": "rows"}' ;;
    captain-data:sqltools) echo '{"tab": "notebooks", "notebook": "shot-sql-tools", "sqlTools": {"panel": "filled"}}' ;;
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
  [[ "$view" == captain-data:notebook || "$view" == captain-data:chart ]] && sleep 25
  [[ "$view" == captain-data:import* ]] && sleep 8
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
done
exit $fail
