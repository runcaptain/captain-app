#!/bin/bash
# CAPTAIN: a real-window check for team views (captain/views/<name>/, plan 0 S8), built on forms-check.sh's window
# handling. A scratch project holds two views made from the template (backend/core/captain_search/view_template). The check
# proves they load and draw, a saved change reaches the app, a broken view keeps its last good version without
# touching the other, and a removed view goes away.
#
#   tests/mod-render/team-views-check.sh [captain-app binary]      default: target/debug/captain-app
#
# (forms-check.sh's header follows, for the shared window handling.)
# (defaults/mods/captain-forms/tasks.js). Neither
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
HOME_DIR=$(mktemp -d /tmp/cteam.XXXX)
WORK=$(mktemp -d)
OUT=${SHOT_DIR:-$WORK/shots}
SESSION=test-team-$$
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

echo "seeding a scratch workspace and project"
CST_HOME=$WORK/cst "$CST" init test --storage "$WORK/store" >/dev/null 2>&1
CST_HOME=$WORK/cst "$CST" demo seed >/dev/null 2>&1
PROJECT=$WORK/project/captain
for v in rows other; do
  mkdir -p "$PROJECT/views/$v"
  for f in mod.json main.js README.md .gitignore; do
    sed -e "s/{{name}}/$v/g" -e "s/{{title}}/Team $v/g" "backend/core/captain_search/view_template/$f" > "$PROJECT/views/$v/$f"
  done
done
: > "$PROJECT/captain.toml"

printf '{"appearance":"light"}\n' > "$HOME_DIR/settings.json"
printf '{"frame.welcomed":"skipped"}\n' > "$HOME_DIR/shared.json"
printf '{"state":"windowed","bounds":[80,80,1100,750]}\n' > "$HOME_DIR/window.json"
CAPTAIN_TEST_WINDOW=1 CAPTAIN_WINDOW_TITLE="Captain (test)" CAPTAIN_APP_HOME=$HOME_DIR CAPTAIN_CORE_SESSION=$SESSION HC_SESSION=$SESSION CST_BIN=$CST CST_HOME=$WORK/cst CST_PROJECT=$PROJECT "$BIN" >"$WORK/app.log" 2>&1 &
APP_PID=$!
for _ in $(seq 1 60); do [[ -S $HOME_DIR/captain-app.sock ]] && break; sleep 0.5; done
[[ -S $HOME_DIR/captain-app.sock ]] || { echo "the window did not start: $(tail -5 "$WORK/app.log")"; exit 1; }
app() { CAPTAIN_APP_HOME=$HOME_DIR "$BIN" "$@"; }
overlays() { app status | python3 -c "import json,sys; print(' '.join(o for o in json.load(sys.stdin).get('overlays', []) if o != 'toast'))"; }
errors() { app status | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin).get('errors', [])))"; }
close_all() { for _ in 1 2 3; do local open; open=$(overlays); [[ -z "$open" ]] && return 0; for n in $open; do app do mod.open "{\"name\":\"$n\"}" >/dev/null 2>&1 || true; done; sleep 0.6; done; }
staged() { [[ -f "$HOME_DIR/mods/team-$1/main.js" && -f "$HOME_DIR/mods/team-$1/kit/cst.js" ]]; }
wait_for() { for _ in $(seq 1 40); do eval "$1" && return 0; sleep 0.5; done; return 1; }
shoot() { close_all; app do mod.open "{\"name\":\"$1\"}" >/dev/null; sleep 3; app shot "$OUT/$2.png" >/dev/null; SHOWN=$(overlays); close_all; }
drawn() { [[ "$SHOWN" == *"$1"* ]] && python3 tests/mod-render/forms-shot.py "$OUT/$2.png" --any; }
fail=0
ok() { echo "ok: $1"; }
bad() { echo "FAILED: $1"; fail=1; }

# 1. Both views load (copied in with the kit) and draw.
wait_for "staged rows && staged other" && ok "both views loaded with the kit" || bad "views not loaded ($(ls "$HOME_DIR/mods" | tr '\n' ' '))"
sleep 3
shoot team-rows rows-1; drawn team-rows rows-1 && ok "team-rows draws" || bad "team-rows didn't draw ($SHOWN) $OUT/rows-1.png"
shoot team-other other-1; drawn team-other other-1 && ok "team-other draws" || bad "team-other didn't draw ($SHOWN)"
python3 - "$HOME_DIR/shared.json" <<'PY' && ok "the sidebar lists them" || bad "the sidebar's list is wrong"
import json, sys
v = json.load(open(sys.argv[1])).get("captain.team.views") or []
assert sorted(x["mod"] for x in v) == ["team-other", "team-rows"], v
assert all(not x.get("problem") for x in v), v
PY

# 2. A saved change reaches the app.
sed -i '' 's/Team rows/Team rows, edited/' "$PROJECT/views/rows/main.js"
wait_for "grep -q 'Team rows, edited' '$HOME_DIR/mods/team-rows/main.js'" && ok "a saved change is copied in" || bad "the change wasn't copied"
sleep 2
[[ -z "$(errors)" ]] && ok "it reloaded without errors" || bad "errors after the change: $(errors)"

# 3. A broken view keeps its last good version, says why, and doesn't touch the other.
cp "$PROJECT/views/rows/main.js" "$WORK/rows-good.js"
printf 'export default class {\n' > "$PROJECT/views/rows/main.js"
problem() { python3 -c "import json,sys; v={x['mod']:x for x in (json.load(open(sys.argv[1])).get('captain.team.views') or [])}; p=v.get('team-rows',{}).get('problem'); print(p or ''); sys.exit(0 if p else 1)" "$HOME_DIR/shared.json"; }
wait_for "problem >/dev/null" && ok "the sidebar says why team-rows can't load: $(problem | cut -c1-90)" || bad "no problem reported for team-rows"
grep -q 'Team rows, edited' "$HOME_DIR/mods/team-rows/main.js" && ok "its last good version stays installed" || bad "the broken file replaced the last good version"
shoot team-rows rows-2; drawn team-rows rows-2 && ok "team-rows opens its last good version" || bad "team-rows is blank ($SHOWN) $OUT/rows-2.png"
shoot team-other other-2; drawn team-other other-2 && ok "team-other still draws" || bad "team-other broke too"
cp "$WORK/rows-good.js" "$PROJECT/views/rows/main.js"
wait_for "! problem >/dev/null" && ok "fixing it clears the problem" || bad "still a problem after the fix: $(problem)"

# 4. A view removed from the project goes away.
rm -rf "$PROJECT/views/other"
wait_for "[[ ! -d '$HOME_DIR/mods/team-other' ]]" && ok "a removed view goes away" || bad "team-other is still installed"

[[ $fail == 0 ]] && echo "team views: all good" || echo "shots: $OUT"
exit $fail
