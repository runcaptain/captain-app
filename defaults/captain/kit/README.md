# captain-ui: the kit Captain's native screens are built from

Every Captain mod carries this folder as `./kit/` (gpui-kit mods can't import outside their own folder).
Edit it here, in defaults/captain/kit, then run `scripts/captain-kit-sync`, which copies it into every
`defaults/mods/captain-*/kit/` and `defaults/frame/welcome/kit/`. tests/captain_mods.rs fails if a copy differs.

- `tokens.js`: Captain's colors (the Captain Search desktop app's index.css), light and dark, and fonts.
- `cst.js`: the daemon. `api(path, {method, body})` resolves `{ok, status, data, error, starting}` and never
  rejects. The base URL and token come from shared "captain.daemon", which the app keeps current.
- `icons.js`: `icon(name)`, the home path of a shared Lucide glyph (defaults/icons/captain-*.svg), for `svg()`.
- `appearance.js`: `getAppearance()` and `setAppearance("light" | "dark" | "system")`.
- `ui.js`: Card, Badge, Button, Tabs, Empty, ErrorState, Loading, Bars, Line, and Table (a fixed list, or
  server paging with `fetch({offset, limit, sort, desc})` returning `{rows, total}`).

Rules from the 100,000-row spike (docs/captain/RISKS.md): never hold or sort a whole result set in a mod. Ask the
daemon for pages and pre-totalled series. A render must stay well under 50 ms.
