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

## Webview blocks (step 3): built and unit-tested, never seen on screen

Nothing below has been checked in a running window: Edgar's Herdr and Captain Search Tools were running, so
no Captain window was opened. The first supervised launch must check each of these.

- Placement: the view is a WKWebView child of GPUI's NSView, placed from the block's GPUI bounds (logical
  px, top-left origin). wry flips y when the parent view isn't flipped (wry wkwebview/mod.rs window_position).
  If the page shows up mirrored vertically or offset by the title bar, this is where to look.
- Overlays draw under native views. The fix is to hide a web view while a dialog, popup, overlay window or
  floating block overlaps it. So the page blanks while the palette is open, and a drawer that peeks over a
  web view hides it the whole time. Toasts don't hide it, so a toast over a web view is not seen. A snapshot
  (WKWebView takeSnapshot) drawn by GPUI while covered would look better, but wry doesn't expose it.
- Sync runs in a paint callback, so a view only moves when GPUI paints. Herdr's idle window doesn't paint
  every vsync (vendor/gpui-pre-macos patch). A resize or layout change paints, but anything that moves a
  block without a repaint would leave the page behind.
- Keyboard: while the page has focus, GPUI gets no keys. A page script hands back the escape hatch and
  global/layout keys, and the window replays them with dispatch_keystroke, then calls focus_parent. Mod-section
  keys and chords are not handed back. A page that stops propagation in its own capture listener first could
  swallow a key, but our listener is installed before page scripts run.
- Clicking back into GPUI should take the keyboard back, because AppKit makes a clicked view that accepts first
  responder the first responder. Unverified.
- `captain-app shot` renders GPUI's frame only, so web views are missing from screenshots.
- Each OS window keeps its own web views. Two windows showing the same layout get two pages.
- Linux and Windows show "web views need macOS in this build" in the block.
- wry adds ~70 crates to Cargo.lock (most for other platforms) and 3 MPL-2.0 entries to THIRD-PARTY-NOTICES.

## Daemon and page messages (step 4)

- Captain reuses a daemon listed in serve.json when its pid lives and its port answers, and never stops one it
  didn't start. If that daemon goes away while Captain runs, the pages show a load error until a reload; the
  state isn't re-checked.
- wry gives pages only `window.ipc`. The page script adds `window.webkit.messageHandlers.captain` as a relay.
  If WebKit refuses the extra property, `captain:navigate` silently does nothing: check on first launch.
- Live theme changes (`captain:theme`) are not sent yet. Pages load with `theme=system`.
- OSC 7501 (agent status): untouched. src/model.rs, src/programs.rs, src/terminal.rs and src/vt.rs are
  identical to upstream.

## Native spike: 100,000 ranked results and a chart in a gpui-kit mod (2026-11-06)

The mod is docs/captain/spikes/ranked-results (not shipped). It passes `captain-app check`. The numbers come from
headless runs of the release build on Edgar's M-series Mac, timed inside the mod with Date.now(). Smoothness on
screen is still unmeasured, because no window was opened.

gpui-kit stops any script call that runs too long (vendor/gpui-kit/crates/shell/src/engine/quickjs/sandbox.rs:439):
50 ms for render and layout, 500 ms for an event handler or init. The checker hit that limit for real.

| Work, 100,000 rows | Time | Fits |
|---|---|---|
| Show 40 visible rows (v_virtual_list, per frame) | 0.7 ms | yes, about 70 times under the 50 ms frame limit |
| Make the rows in memory | 230 ms | in init, yes. In render, no |
| Average a metric over all rows (the chart) | 35 ms | only outside render |
| Sort the objects by a number | about 350 ms (60 ms at 20,000, n log n) | barely, in a click. The UI freezes for a third of a second |
| Sort an index over a typed array | about 140 ms (24 ms at 20,000) | in a click, yes |
| Parse the rows from JSON (13 MB) | about 425 ms (212 ms at 50,000) | barely. Make plus sort, or parse plus sort, is stopped |

What this means for native Captain screens:
- Scrolling a 100,000-row table is cheap on the script side. Only the visible rows are built, at under 1 ms per
  frame. Whether GPUI then draws it smoothly needs the window: check it on the next launch.
- Charts are cheap to draw (12 bars). The aggregate behind them is not cheap to compute in a render.
- Never hold or sort a full result set in the mod. The daemon should sort, filter and aggregate, and send pages
  plus precomputed chart series. One JSON parse of 100,000 rows uses most of a call's time limit on its own.
- When a mod must sort locally, sort an index over typed arrays, or split the work across timer ticks.
- The 50 ms and 500 ms limits are fixed in vendored gpui-kit, not settings. Raising them would be a fork change,
  and a long call blocks the whole window anyway, because mods run on the UI thread.

## Packaging (2026-11-06)

- Captain.app is 528 MB: pyarrow 126 MB, DuckDB 48 MB, pandas 45 MB, the app 57 MB, hc 7 MB. CPython's test suite,
  IDLE, Tk and pip are pruned. Slimming further means changing captain-search's dependencies.
- The whole bundle is signed ad hoc with --deep. It runs on the Mac that built it. Shipping to others needs a
  Developer ID signature, hardened runtime entitlements for the bundled Python, and notarization.
- The packaged hc replaces herdr-core's own signature with the ad-hoc one. Captain runs it in its own session, so
  Herdr's core and binary are unaffected.
