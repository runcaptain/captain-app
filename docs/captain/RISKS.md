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
