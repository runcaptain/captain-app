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

# A short home: the window's control socket must fit in a Unix socket path.
HOME_DIR=$(mktemp -d /tmp/cshot.XXXX)
WORK=$(mktemp -d)
OUT=${SHOT_DIR:-$WORK/shots}
mkdir -p "$OUT"
APP_PID=""
cleanup() {
  [[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null || true
  sleep 1
  # The window starts its own cst serve on our workspace: stop that one, by pid, and nothing else.
  for p in $(pgrep -f "cst serve" || true); do
    if ps eww -p "$p" | tr ' ' '\n' | grep -qx "CST_HOME=$WORK/cst"; then kill "$p" 2>/dev/null || true; fi
  done
  rm -rf "$HOME_DIR"
}
trap cleanup EXIT

echo "seeding a scratch workspace"
CST_HOME=$WORK/cst cst init shot --storage "$WORK/store" >/dev/null 2>&1
CST_HOME=$WORK/cst cst demo seed >/dev/null 2>&1

# Skip the first-start welcome (it opens over everything while shared "frame.welcomed" is unset).
echo '{"frame.welcomed":"skipped"}' > "$HOME_DIR/shared.json"
launch() {
  CAPTAIN_APP_HOME=$HOME_DIR HC_SESSION=test-shot-$$ CST_HOME=$WORK/cst "$BIN" >"$WORK/app.log" 2>&1 &
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

fail=0
for m in "${MODS[@]}"; do
  block=$(block_of "$m")
  [[ -n "$block" ]] || { echo "$m: no section block known"; fail=1; continue; }
  rm -rf "$HOME_DIR/mods/$m"
  mkdir -p "$HOME_DIR/mods"
  cp -R "defaults/mods/$m" "$HOME_DIR/mods/$m"
  rm -f "$HOME_DIR/mods/$m/gpui-kit.d.ts" "$HOME_DIR/mods/$m/jsconfig.json"
  python3 - "$HOME_DIR/layouts/captain.json" "$block" "$m" <<'EOF'
import json, sys
p, block, mod = sys.argv[1:]
d = json.load(open(p))
def find(n):
    if n.get("id") == block: return n
    for c in n.get("children", []):
        r = find(c)
        if r: return r
n = find(d)
n["content"] = {"mod": mod}
json.dump(d, open(p, "w"), indent=2)
EOF
  sleep 2
  app do layout.select "{\"stack\":\"sections\",\"child\":\"$block\"}" >/dev/null
  ok=0
  for i in $(seq 1 12); do
    sleep 1.5
    app shot "$OUT/$m.png" >/dev/null
    if python3 tests/mod-render/shot-busy.py "$OUT/$m.png"; then ok=1; break; fi
  done
  errs=$(app status | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('errors', [])))")
  if [[ $ok == 1 && $errs == 0 ]]; then echo "$m: drawn ($OUT/$m.png)"; else echo "$m: BLANK or errors ($errs) ($OUT/$m.png)"; app status; fail=1; fi
done
exit $fail
