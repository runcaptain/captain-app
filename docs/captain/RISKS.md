# Captain on the Herdr fork: hard or risky things, as found

- Shared herdr-core: the app stores its window document in the core as `herdr-app` (src/mirror_sync.rs:76, 94).
  Two apps on one core session would share spaces and tabs and overwrite each other. herdr-core isolates by
  `HC_SESSION` (state in ~/.local/state/herdr-core/<session>/). Captain must set HC_SESSION=captain.
- `app.update_core` swaps the shared ~/.local/bin/herdr-core and restarts it. In the fork that would restart
  Edgar's Herdr core too. Disable core updates in the fork.
- Core missing: upstream downloads from releases.herdr.dev (src/core_bin.rs:173). The fork must not, so it
  shows install instructions instead.
- Escape hatch must be window-level, never a system-wide shortcut (lead, 2026-11-06, after the Tauri version
  grabbed ctrl-alt-shift-escape globally). With a web view focused, keys go to WebKit, not GPUI: forward
  the hatch and global keys from the web view back to the window.
- Web view over Metal: a WKWebView subview sits above GPUI's Metal layer, so every GPUI overlay (palette,
  dialogs, popups, floating blocks, toasts) draws under it. Hide or snapshot it while covered.
- Shared core binary, step 2: Captain runs its own core session but the same ~/.local/bin/herdr-core binary.
  If Captain ever needs a newer core than Herdr's, the person has to run `hc update`, which swaps the binary
  Herdr uses too. The setup screen says so instead of doing it.
- Inherited HC_SESSION: a terminal inside Herdr exports Herdr's session. `brand::pin_core_session` replaces
  anything but `test-*` with `captain`, so launching captain-app from a Herdr terminal cannot join Herdr's core.
- THIRD-PARTY-NOTICES hashes Cargo.toml and Cargo.lock. Any dependency change in the fork needs
  `just notices`, which needs `cargo install cargo-about --version 0.9.3 --locked --features cli`.
- Herdr's CHANGELOG.md still ships as What's New, with herdr.dev media links (not fetched). Captain needs its
  own notes before anyone else installs it.
- Linux and Windows identity (desktop file, Start menu, .ico install scripts) is not rebranded yet. Only macOS
  was the target.
