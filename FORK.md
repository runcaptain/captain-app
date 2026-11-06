# Captain: a fork of Herdr

Captain is a modified version of Herdr (https://github.com/herdrdev/herdr-app), Apache 2.0. This file lists what
the fork changes and how to take upstream changes. Risks and hard parts are in docs/captain/RISKS.md.

## Rules for fork changes

- Every identity (name, id, path, endpoint) lives in `src/brand.rs`. Upstream code points at it instead of
  spelling a name, so an upstream change to the same line is a one-line conflict.
- Mark each changed spot in upstream files with a `CAPTAIN:` comment. `git grep CAPTAIN:` lists them all.
- New Captain features go in new files where possible.
- Never push to `upstream`, open pull requests there or file issues there.

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

**The webview patch** (one isolated feature; retire it as screens are ported to native mods). Every file it
touches:
- New: `src/webview.rs` (the views, URL rule, key hand-back, page messages), `src/cst.rs` (the daemon).
- `src/layout.rs`: the `Content::Webview` variant and `WebviewSpec`.
- `src/window.rs`: the `webviews` field, the `Content::Webview` arm of `render_block`, `render_webview`,
  `webview_pass` (the root's last child), a `cover:<id>` canvas on each overlay, `Request::Keystroke` handling,
  and the `webview.reload` / `webview.devtools` commands.
- `src/mods.rs`: the `Request::Keystroke` variant. `src/commands.rs`: the two commands.
- `src/settings.rs`: `webview_hosts`. `src/main.rs`: the module lines and `cst::stop()` at quit.
- `Cargo.toml`, `Cargo.lock`, `THIRD-PARTY-NOTICES`: wry 0.57.1 (macOS only) and url.
- `defaults/AGENTS.md`: the block, the commands and the setting.

What it does: `{"webview": {"url", "id"}}` puts a WKWebView over a block. Localhost only, plus settings.json
`webview_hosts`. Hidden while a dialog, popup, overlay window or floating block covers it. The page hands the
escape hatch and global/layout keys back to the window (window-level, never a system shortcut). A page may post
`{type: "captain:navigate", screen}` to `window.webkit.messageHandlers.captain` to switch the `sections` stack.
`{cst}` and `{cst_token}` in a URL come from the Captain Search daemon: a live one from `serve.json` is reused
(and never stopped), else Captain starts `cst serve --port 0` with a guard process and stops it at quit.

**The Captain window:** `defaults/layouts/captain.json` and the `captain-nav` mod (Investigations, Data and
signals, Runs, Comparisons, Handoff, a Terminals toggle that hides the shells without ending them, Settings).
It's the default layout and the escape hatch's target (`brand::DEFAULT_LAYOUT`). Herdr's `main` ships as
"Spaces". New sections are built as native gpui-kit mods first; the web view only carries existing screens.

**Visible strings in shipped mods and frame screens** say Captain and show the Captain icon
(`frame/welcome/captain.png`): welcome, settings, palette, What's New, help, menu, feedback, the spaces and
workspace-chrome mods, machine-add. These are single-word edits in upstream files, because the strings are
literals there; expect small merge conflicts in them.

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
