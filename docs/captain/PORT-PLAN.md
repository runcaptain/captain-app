# Porting Captain's content to native mods

Rule (Edgar, 2026-11-06): Captain has exactly one of each piece of chrome, and it's always the native one. The
sidebar, terminals, palette, New menu, settings, dialogs, toasts and theme are Herdr's native parts (or Captain
mods built from them). The webview block only carries content, loaded with `?embedded=1&screen=<id>`, until each
screen is ported. Then its block's content changes from `{"webview": ...}` to `{"mod": ...}` in
`defaults/layouts/captain.json`, and the webview patch goes away with the last one.

What the 100,000-row spike settled (docs/captain/RISKS.md): a native screen asks the daemon for pages, sorts and
precomputed chart series. It never holds a whole result set, because a script call is stopped after 50 ms in render
and 500 ms in an event.

## Order

1. **Investigations, list and detail** (home). On GET /api/investigations and its detail routes (captain-ide
   docs/api.md, "Investigations, signals and decisions"). List: filter by state, search. Detail: the hypothesis with
   its revisions, evidence, signals, runs with a status each (failed ones visible), the decision, and Copy prompt
   (GET /prompt, then the clipboard). Agent turn, job and investigation are separate states: show each from its
   own record, never infer one from another. Records made with CST_AGENT show the agent.
2. **Comparison view.** Metrics, slices, queries side by side and the limits of a result. The slice chart is bars
   from a precomputed series. The query table is a virtual list over a paged route.
3. **Data grid.** After the spike: v_virtual_list over daemon pages with server-side sort and filter. Needs a paged
   route if the current one returns whole tables (check before starting).
4. **Runs.** The list with status, then a run's manifest, logs (paged, newest last) and artifacts.

Handoff stays a webview until these four are done.

## For each port

- A `captain-<name>` mod in defaults/mods with its own copy of defaults/captain/cst.js (tests/captain_mods.rs).
- `captain-app check` passes, and a render test covers its empty and populated states.
- The section's block in captain.json switches to the mod. The page's own nav stays hidden (embedded mode), so no
  chrome is duplicated during the switch.
- The branding test passes, and nothing new reaches herdr.dev.
