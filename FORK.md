# Captain: a fork of Herdr

Captain is a modified version of Herdr (https://github.com/herdrdev/herdr-app), Apache 2.0. This file lists what
the fork changes and how to take upstream changes. Risks and hard parts are in docs/captain/RISKS.md.

## Rules for fork changes

- Every identity (name, id, path, endpoint) lives in `src/brand.rs`. Upstream code points at it instead of
  spelling a name, so an upstream change to the same line is a one-line conflict.
- Mark each changed spot in upstream files with a `CAPTAIN:` comment. `git grep CAPTAIN:` lists them all.
- New Captain features go in new files where possible.
- Never push to `upstream`, open pull requests there or file issues there.

## The backend lives here

`backend/` is Captain's own code (the Captain Search backend and the `cst` CLI), moved in from a separate repo on
2026-11-06 so one repo holds everything. It is not from Herdr and never goes upstream. See backend/README.md.

## What we changed

Identity (Apache 2.0 section 6 grants no trademark rights):
- Name "Captain" in window titles, the macOS menu, the setup screen and the update toast.
- Executable and CLI `captain-app` (`[[bin]]` in Cargo.toml). The Cargo package keeps the name `herdr-app`, so
  Cargo.lock and upstream merges stay quiet.
- Bundle id and app id `dev.captain.app` (`CAPTAIN_APP_ID` overrides the app id).
- Icon: the Captain C (`assets/icons/Captain.icns`, `captain-app.ico`, `captain-ui-icon.png`). Herdr's icon files
  stay in the tree for merges and are not used by Captain builds.
- An About panel (`macos.rs`) that shows the notice above.
- `defaults/AGENTS.md` and `src/SKILL.md` say Captain and `captain-app`.

Side by side with a real Herdr:
- Home `~/.config/captain-app` (`CAPTAIN_APP_HOME` picks another). `HERDR_APP_HOME` is not read, so a shell set
  up for Herdr cannot point Captain at Herdr's home.
- Control socket `captain-app.sock` in that home.
- Its own herdr-core session, `captain` (`CAPTAIN_CORE_SESSION` picks another). Herdr stores its window in the
  core as a document named `herdr-app`, so two apps on one session would share and overwrite spaces and tabs.
  `brand::pin_core_session` sets `HC_SESSION` at the top of `main`, so every `hc` the app starts reaches
  Captain's session. A `test-*` session is kept, so upstream's tests and sandbox still isolate themselves.
- The macOS CLI link is `~/.local/bin/captain-app`, pointing at Captain.app. Herdr's `herdr-app` link is not
  touched.

Never contacts herdr.dev:
- Telemetry and feedback have no default endpoint (`CAPTAIN_TELEMETRY_URL`, `CAPTAIN_FEEDBACK_URL`).
- App updates run only against `CAPTAIN_RELEASES_URL`, which Captain does not have yet.
- herdr-core is never updated or downloaded by Captain. The binary is shared with Herdr, and `hc update` swaps
  it and restarts the core. The setup screen shows the core's own install command for the person to run.
- Release-note media is not fetched. The bundled notes are Herdr's and link to media.herdr.dev.

Captain features (new files where possible):

**The webview patch was removed** (2026-11-06): every section became a native screen, so wry, `src/webview.rs`,
the `{"webview": ...}` block, its commands and `webview_hosts` are gone, and the fork delta with them.
`src/cst.rs` stays: it starts or reuses the Captain Search backend and tells mods where it is.

**The Captain window:** `defaults/layouts/captain.json` and the `captain-nav` mod (Investigations, Data and
signals, Runs, Comparisons, Handoff, a Terminals toggle that hides the shells without ending them, Settings).
It's the default layout and the escape hatch's target (`brand::DEFAULT_LAYOUT`). Herdr's `main` ships as
"Spaces". New sections are built as native gpui-kit mods first; the web view only carries existing screens.

**Native Captain parts** (mods; no fork code beyond the palette hook):
- `captain-nav`: the sidebar in the Captain Search desktop app's look (its NavItem and index.css tokens, Inter
  shipped in the mod), with workspace name, New, five sections, badges, Agents, Terminals, Settings and a rail.
  Props reorder or add items.
- `captain-commands`: every cst command as a native form (GET /api/commands, POST /api/commands/run), and the New
  menu. The palette lists them under "Captain".
- The onboarding replaces Herdr's welcome frame (`defaults/frame/welcome/main.js`): three interface pages and a
  live checklist from GET /api/onboarding.
- `defaults/captain/cst.js` is the daemon client every Captain mod copies (mods can't import across folders).
- Palette hook (`defaults/frame/palette/main.js`): entries mods publish under shared `palette.extra`, and entries
  that open an overlay.
- Keys: a `layout:captain` section in `defaults/keys.macos.json` (cmd-k palette, cmd-j shells, cmd-b sidebar). The
  upstream 0.1.13 file is kept as a shipped original, so untouched keys files upgrade.
- What gets ported next, and in what order: docs/captain/PORT-PLAN.md.

**Visible strings in shipped mods and frame screens** say Captain and show the Captain icon
(`frame/welcome/captain.png`): welcome, settings, palette, What's New, help, menu, feedback, the spaces and
workspace-chrome mods, machine-add. These are single-word edits in upstream files, because the strings are
literals there; expect small merge conflicts in them.

**No Herdr branding a person can see** (`tests/branding.rs` enforces it). It scans every shipped mod, frame,
layout, theme and doc, `src/SKILL.md`, and Rust string literals outside tests. It fails on /herdr/i except the
allowlist at its top: the notice, the `herdr` mod module and `herdr.d.ts`, `herdr-core` (the terminal engine's
real binary name; prose calls it "the terminal engine (hc)"), `herdr.<label>` core labels, `HERDR_*` variables
and GPUI key-context names. "Herdr 98" (`win98`) isn't shipped (build.rs `NOT_SHIPPED`). Upstream's icon files
keep their names, so their release scripts work, but hold the Captain C. One visible herdr.dev URL remains: the
terminal engine's install command on the setup screen, until backend/install.sh installs hc.

**Captain.app carries what it runs** (`scripts/package-captain-macos`): a python-build-standalone CPython (through
uv) with captain-search installed from `backend/core` in this repo, with every database driver, in
`Contents/Resources/cst/`, with a `bin/cst` launcher that works through a symlink; and the herdr-core
(hc, Apache-2.0) binary with its `--licenses` output in `Contents/Resources/hc/`. Captain starts the packaged cst
and hc (`brand::bundled`). settings.json `cst` / `core` or `CST_BIN` point elsewhere for development. Every terminal
Captain opens gets that same cst: its folder first on PATH and its path in `CST_BIN` (`cst::terminal_env`), and every
prompt handed to an agent names the path, because an older cst in `~/.local/bin` lacks the commands prompts use. At launch it
offers the packaged cst as `~/.local/bin/cst` only when there's none, or the link already points into a
Captain.app: a cst the person installed is never replaced, and `~/.local/bin/herdr-core` is never touched.
Captain's services are never named to the person: no "engine" or "daemon" in visible text (tests/branding.rs).

Kept as upstream, on purpose:
- The mod module name `herdr` and `herdr.d.ts`, so upstream mods run unchanged.
- Internal key contexts (`Herdr`), the core document name `herdr-app` (inside Captain's own session) and most
  internal `HERDR_*` test variables.
- CHANGELOG.md (Herdr's notes, shown as What's New). Captain needs its own before a release.

## Taking upstream changes

```sh
git fetch upstream
git checkout captain
git merge upstream/master        # merge, never rebase: the branch is shared
git grep -n 'CAPTAIN:'            # check each fork change survived
git grep -n -i 'herdr\.dev' -- src ':!src/update_notes.rs'   # nothing new should call herdr.dev
just build && just test
```

Check by hand after a merge:
- New network endpoints in upstream (`git diff ORIG_HEAD -- src | grep -n https://`).
- New user-visible strings that say "Herdr".
- New uses of `HERDR_APP_HOME`, `herdr-app.sock` or the core session.
- Changes to `defaults/AGENTS.md`: keep the Captain naming.

Run tests with a scratch home and core session, so nothing reaches a running Herdr or Captain:

```sh
CAPTAIN_APP_HOME=$(mktemp -d) HC_SESSION=test-captain XDG_STATE_HOME=$(mktemp -d) just test
```
