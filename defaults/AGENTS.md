# Captain app

Captain is the open-source platform for building better search and recommendation models: from signal discovery to reproducible experiments and measured production improvements.

Captain is a modified version of Herdr (https://github.com/herdrdev/herdr-app), Apache 2.0.

<!-- Written by the Captain app on every start. Edits here are overwritten. -->

This folder is **the Captain app's whole interface, as files**. The Captain app
is a desktop app for terminals and coding agents, and it redraws itself from
these files while it runs: change a layout, a key or a panel here and the
window follows within a second. Shipped layouts and mods are Captain's:
change settings for look, or clone before editing structure/code (see Layouts).
When someone asks for something ("a sidebar
of my PRs", "give ctrl+1 back to the shell", "make it warmer", "move the
agents list to the right"), you do it by editing these files or writing a
mod. Nothing in the default interface is special: the spaces sidebar, the
agents list, the tab bar, menus, dialogs and notifications are built
from the same parts you have.

## Two programs

- **`captain-app`**, the window: spaces, tabs, layouts, panels.
- **`hc`**, which runs every terminal and agent. They keep
  running when the window closes or updates. You can use `hc` yourself:
  start a terminal, read its screen, send it text, wait for an agent
  (`hc --help`). The Captain app is one client of it.

`captain-app` controls this app; `hc` controls the terminals.

## What you can and can't do

You can: rearrange and restyle the whole window (layouts), rebind any key,
write panels (mods) that list, filter and act on spaces, tabs, agents
and terminals, open dialogs, popups and floating blocks, show a live
terminal inside a block, run any app command, run programs (`exec`) and
fetch URLs, keep state per mod or shared between mods, animate natively.

You can't: use HTML, CSS, a web view, Node APIs or `setTimeout`; draw
outside your block except through overlays; change how a terminal itself
draws text; read or change other mods' code at run time.

## What mods are written in

Mods use **gpui-kit** (its gpui-shell): JavaScript that describes **GPUI**
elements, a native GPU UI (`div()`, flex, children,
`.on_click`). There's no DOM: the script describes, Rust draws. Background:
https://gpui-kit.com. The Captain app carries its own copy with additions
(`frames`, `loop`), so **the type files here are the truth**:
`gpui-kit.d.ts` for elements and `herdr.d.ts` for the app's data and
commands. They're rewritten on every start to match this version.

## Changing things safely

- Edits apply at once. If a file is invalid, the app keeps the last good
  version and writes why to `<name>.error` (`layout.error`, `keys.error`,
  `settings.error`, `programs.error`). Check after every edit.
- `captain-app check <mod folder>` loads a mod once and checks its initial
  description and eager native elements, including virtual lists and motion.
  It does not lay out the mod or run deferred rows, nested renders, or later
  async states. Never call a mod done without a passing check; also try it
  in the app.
- **Escape hatch:** `ctrl-alt-shift-escape` always works: it sets the shown
  layout and `keys.json` aside (`*.reset-<time>`) and goes back to the
  default `captain` layout. Nothing can rebind it, and it works while a web
  page has the keyboard.

## Seeing the window

`captain-app shot [file.png]` saves the last rendered window frame as a PNG
and prints its absolute path (default: `captain-shot.png` in your current
folder). Use it when debugging mod UIs: inspect spacing, colors, terminal
images, overlays, popups and floats. It captures the app, not the desktop,
at the window’s physical pixel size, even when covered or minimized. No
screen-recording permission is needed. Captain must be running; before its
first frame or during GPU recovery it reports an error instead of a PNG.
`CAPTAIN_APP_HOME` selects which running window to ask, as with `status`.

## What an agent can see and do

**See.** When the person says "this", "here" or "what I'm looking at", or names nothing, look before you ask:

- `cst context show` prints what they have open in Captain right now and what they pinned for you. The screen
  part: the workspace, the project, the screen (in the screen's word, with its section id), the selected item (a
  dataset, a run, an experiment, a launch review), an open form with its filled fields, and the page's "Next:"
  line with its action, then what Captain's services know about those things. The pinned part: each thing with a
  short summary (a dataset's kind, rows, columns and three sample rows, a run's status and metrics, a project's
  inputs and next step). `--json` for a record, `--bare` for names only. With Captain closed it still lists pins.
- `cst app context` is the screen part alone, and exits 1 with one sentence when Captain is closed.
- `cst app shot [file.png]` saves a picture of the window and prints its path. With no path it goes under the
  app's own folder (`cache/shots/`, the newest 20 are kept), never your working folder.
- `cst context add <ref>...` pins more: `dataset:<name>[@vN]`, `model:<name>@vN`, `run:<evaluation>`,
  `experiment:<id>`, `candidate:<project>/<id>`, `table:<connection>.<table>`, `project:<id>` or a file path. A
  bare name is looked up. `cst context rm <ref>` and `cst context clear` remove. People pin with "Send to agent"
  on a dataset, an input card, a candidate, a comparison, a run or an experiment: that also puts a one-line
  reference in the open agent tab's input. The sidebar's Agent context strip lists the screen and the pins.
  Pins live per workspace in `context/<workspace>.json` in the app's folder, with who added each and when.

These work in any terminal Captain opened: those carry `CAPTAIN_APP_HOME` (which window) and `CST_BIN` (which
`cst`). Secrets never come out: a filled secret field reads `(hidden)`, a URL loses its login, a file named like
a key file is refused, and a connection is named but never opened.

**Show.** `cst app open <target>` shows the person something: a screen (`home`, `experiments`, `data`, `runs`,
`compare`, `launch-review`, `team`), `project:<id>`, `dataset:<name>` (`--tab sample`), `run:<evaluation>`,
`comparison:<evaluation>[@vN]`, `experiment:<id>`, `settings[:<section>]`, or `form:<task> --value field=value`.
A form opens filled in for the person to read and confirm. It is never submitted for them, and a secret is never
filled in. A record outside the Explorer's pick opens by showing the whole workspace, with a note saying so.

**Do.** Propose first. When the person says to apply it, apply it and quote them:
`cst projects accept <project> <proposal> --as-asked "<their words>"`. Never apply on your own: without
`--as-asked` an agent's accept is refused. `cst projects slot-add`, `slot-edit`, `slot-rm`, `input`, `input-clear`,
`pin`, `kind` and `evaluation` work for an agent when the person asked. Each change an agent makes is written to
the project's history with the agent's name, the time and, for an accept, the person's instruction; the proposal
keeps `applied_by` and `on_instruction` too. Rejecting a proposal, deleting a project, deleting data and
overriding a live launch review stay with the person, in the app.

Mods add to what is seen with the kit's `view.js`: `watchView(cx, "<section id>", () => ({ tab, dataset, ... }))`
in `init` keeps shared `captain.view.<section id>` current from a timer, written only when it changes (never call
it from render). A form over the page publishes `"dialog"` with `safeFields(values, secretKeys)`. `context.js` has
`SendToAgent(c, id, { ref, kind, label })` for a screen's own things. `askAgent` and `copyPrompt` (kit `agent.js`)
put the screen at the top of every prompt, and the palette's "Ask an agent about this screen" (also the button
beside Agents in the sidebar) sends it to the open agent tab, or opens one. `captain-app view` is the window's own
answer, unjoined.

## The files

| File | What it decides | Applied |
|---|---|---|
| `layouts/<name>.json` | the window and its whole look: blocks, theme, fonts, backgrounds, pane frames; one file per layout (`main`, `terminals`, `win98`, any you add) | live |
| `keys.json` | what every key does | live |
| `settings.json` | the person: zoom, sounds, fonts for every layout | live |
| `mods/<name>/` | panels: small JavaScript programs | live (hot reload) |
| `programs.json` | how programs show (name, icon, tile), on top of the built-in table | live |
| `icons/` | the shipped program icons | written by Captain (put your own elsewhere) |
| `frame/` | the app's own screens (welcome, ...) | rewritten by Captain on every start: don't edit, it won't stick |
| `view.json` | this screen's selected space/tab/pane, zoom, settled tabs, recent spaces and seen attention | written by Captain |
| `shared.json`, `data/`, `seen.json`, `installed.json`, `window.json`, `windows.json` | mod state, bookkeeping, where the window was (size, maximized), mods' windows (spots, kept ones) | written by Captain |

Shared state is saved atomically in the background, with the latest change winning.
If `shared.json` is invalid, Captain logs a warning and keeps it as
`shared.json.corrupt-<time>` before starting empty.
`captain-app check` uses empty shared state in memory and leaves this file untouched.

## The model: spaces, tabs, terminals

Every layout shows the same world; a layout may show less, never a
different one. Each machine keeps its spaces, tabs and splits in its hc's
`captain-app` document. Connected screens share that layout and live terminals,
not their focus, zoom or attention. Use commands to change structure; there
is no `spaces.json`. On simultaneous edits the newest accepted document wins
(no merging); structural commands fail while that machine's layout is unavailable.
Mod pane props are stored as given, never machine-qualified. Space order is local
machine first, then machines by name; `space.move` stays inside its machine's block.
In the expanded Spaces sidebar, drag a space to reorder it within its machine;
the highlighted line shows where it will land.

- A **space** is a place to work, on one machine: this one, or another
  from `hc machine ls` (`spaces()[i].machine`). Captain leaves out machines
  added with `--temp` (an agent's short-lived sandbox): no spaces, not in
  `machines()`, and mods listing machines skip `temporary`. Names work as in Captain:
  a space nobody named is called after where its first terminal is now
  (its repo, else its folder, `~` at home) and follows it; once named
  (`space.rename`), the name stays; renaming to "" goes back.
  `spaces()[i].named` says which. Where a space is now is its terminals'
  live folders (`panes()[i].cwd`), not the folder it was made in: new
  tabs start where its focused pane is, and the default mods take a
  space's repo from its first terminal.
  If starting in an inherited folder fails, new spaces, tabs and splits retry in
  that machine's home. Explicit invalid folders still fail.
- A **tab** is a piece of work in a space: its terminals and how they're
  split right and down. One nobody named is called by its place (1, 2,
  ...), as in Captain; `tab.rename` names it ("fix login"). A space
  has one or more; closing the last one closes the space (it stays in
  `recent`).
- A tab can be **settled** (`tab.settle`): put away on this screen, still running;
  `settled` on each tab. The shipped layouts don't use it (they list every
  tab); a mod that does can hide or fold settled tabs. Showing one, or an
  agent in it needing the user, brings it back.
- A **pane** is a terminal on screen, or a **mod** beside them (a markdown
  viewer, a board, a git panel): `pane.split {direction, mod, props}`. A
  mod pane splits, zooms, closes and gets a header like any pane, keeps
  the keyboard when clicked, and is saved with its tab; its `props` tell
  it what to show (a file, a folder). Closing a terminal pane ends its
  terminal, and a program exiting closes its pane, even if Captain was closed.
  Terminal panes are kept by their machine's hc (`keep: true`). A lost
  terminal (hc crash, reboot, logout) gets a shell in its last folder,
  with the same address; its original command is never run again. Splits
  and mod panes stay. An exited or killed terminal is closed, not restarted.
  Focus, zoom, recent spaces, settled tabs and seen attention stay on this screen.
- Every terminal is a hc terminal. Captain labels the ones it starts with
  `herdr.space=<id>` and `herdr.tab=<id>`. **A terminal with those
  labels shows up there**, even one you start yourself:
  `hc run --keep --label herdr.space=<id> --label herdr.tab=<id> -- claude`.
  Labels use ids local to that machine; remote ids shown by the app have a
  `<machine>/` prefix (strip it when writing labels on that machine). Adoption
  also enables hc keep; `terminal.park` removes the pane and disables keep.
  When a hc change is needed, `terminal.keep` / `terminal.park` acknowledge
  with `pending: true`, then apply after the machine replies; failures show a toast.
  Normal quit waits for pending terminal changes and layout writes; remote waits are bounded.
  An id Captain doesn't know becomes a new space or tab (unnamed: Captain's names).
  Terminals without the labels (another app's) stay out of Captain's
  screens, and so do other machines' terminals, unless Captain started them
  for a space on that machine.
- **Agents** are terminals whose program reports a status (OSC 7501):
  `working`, `blocked`, `idle`, or `done` = finished while the user wasn't
  looking (it becomes `idle` once they look). A space's and a tab's
  status is the most urgent of its agents: blocked > done > working > idle.
- **Sub-agents** are terminals an agent started with `hc run`: hc
  records the starter as `parent` (see `terminals()`). They belong to their
  parent's tab without a pane; the `agents` mod shows them,
  `terminal.peek` opens one, `terminal.keep` gives one a pane.
  Ctrl-click (Cmd-click on macOS) a live terminal address in terminal text to
  peek at it. Bare addresses refer to the machine of the terminal showing them;
  use `machine/address` for another machine. Names and non-live addresses aren't links.
- **Agent navigation order** belongs to the mods. A mod displaying agents calls
  `setAgentOrder(list.map(a => a.address))` on render so `agent.next` /
  `agent.prev` follow its list.

## Layouts: `layouts/<name>.json`

Each file is one whole window. `layout.switch` (`ctrl-shift-l`) cycles
through them, `layout.switch {name}` picks one; `shared("layout")` is the
shown one's name. Captain opens with `captain` ("Captain": the `captain-nav`
sidebar with spaces, Experiments, Data and signals, Runs, Compare and
Launch review; a `sections` stack of native screens, `captain-investigations`
(Experiments), `captain-handoff` (Launch review) and the others; terminal tabs
and panes on the right; see Words for what each screen word is called in the CLI).
Captain's screens read Captain's local services, which it starts at launch;
a mod reaches them through shared `captain.daemon` (`{base, token}`). It also
ships `main` ("Spaces": spaces by machine and the
agents list on the left, `workspace-chrome`; tabs as pills with a + menu
that starts a terminal or any installed agent, `new-menu`; panes as cards
with a header; Files and Changes panels on the right, `files` and
`changes`, opened from the tab bar and remembered per space (which ones:
shared `"workspace.panels"`, set by the welcome); a status bar,
`workspace-status`), and as examples `terminals` ("Terminals": tabs in
the title bar, panes as cards, a space picker). Add a layout by
writing a new file. The Files panel supports Windows folder paths with spaces,
parentheses and apostrophes, and preserves Unicode file and folder names
(including Chinese). Directory links expand as folders; their contents load
only when expanded.

**Captain's own layouts and mods are read-only.** They are replaced whole
on every start. Use settings.json for look (fonts, sizes, theme, colors,
sounds), where the layout leaves them unset. Captain (main) has no look of
its own, so your settings decide it; Terminals brings its own
look. The theme picker overrides any layout. To change a shipped layout's
look beyond that, or its structure/code, clone it and edit the clone:

```sh
captain-app do layout.clone '{"name":"main","as":"my-layout"}'
captain-app do mod.clone '{"name":"files","as":"my-files"}'
```

The default clone name is `<name>-copy`; an existing name is refused.
`layout.clone` switches to the clone. In the palette: Duplicate layout
or Duplicate mod. Clones are yours and never updated. They record
`cloned_from: {name, version}` and copy the layout and its clicks as-is,
including its look. A cloned layout keeps its mod references:
clone a mod too and point your layout's `content.mod` to it to change code.
Mod clones rewrite their own `mods/<name>/` asset paths and self-opening
overlay names to the new name.

If you edit a shipped file anyway, the next start moves it to
`<name>-edited` (a mod folder or layout JSON; a number is added if taken),
copies its layout clicks, restores Captain's version and shows a toast.
No-longer-shipped things go if
untouched, otherwise they stay.

Clicks (sizes, shown/hidden, stack child, floating spots, theme picks,
background, text size, pane header height and border/focus look) live
in `layouts/.state/` and survive restarts. Don't edit those files.
A deliberate layout-file edit beats a click on that field.
`layout.reset {name}` resets a shipped layout and its clicks, keeping its
previous file aside as `<name>.json.old-<time>`.

A layout is a tree of nodes. Four types:

- `block`: shows content. `"content"` is `{ "mod": "<folder>", "props": {...} }`,
  `{ "terminal": "t7" }`, `{ "tab": "active" }` (the shown tab's
  terminals as it arranges them), or `{ "agent": "focused" }` (one agent's
  terminal, full size: the one chosen with `agent.focus` / `agent.next`, else
  the focused pane's agent, else the first agent).
- `row` / `column`: children side by side / stacked.
- `stack`: shows one child (`"active": "<child id>"`), with an optional
  `"header"` (a mod that draws tabs, or `"tabs"` for a plain built-in strip).

Properties any node can have:
- `id` (unique; commands use it), `size`: `{ "px": 240 }` or `{ "weight": 2 }`
  (shares of what fixed siblings leave), `min` / `max` (px), `hidden`.
- Paint: `background` (a color; `{ "gradient": ["#2a6cbd", "#0b2a57"], "angle": 165 }`;
  or `{ "image": "backgrounds/sky.jpg", "fit": "cover" | "contain" }`, a path in
  the home folder), `border` (`{ "width": 1, "color": "border" }`), `radius`,
  `shadow` (`"sm" | "md" | "lg"`), `focus` (`{ "border": {...} }` when the
  focused pane is inside).
- `padding`: space inside the node's edge, px: `16` or `[vertical, horizontal]`.
- row/column: `gap` (px between children), `divider` (`{ "width": 1, "color": "border" }`),
  `resizable` (default true: drag the boundary). This applies to that row/column only.
- `movable` (default true): panes can be dragged by the app's top-center grip within a split tab.
  Unlike `resizable`, it inherits: the nearest explicit ancestor wins, through the
  tab tree and then the layout's tab block and its ancestors. Root `"movable": false`
  opts the layout out. Composition terminal blocks aren't tab panes and never drag.
  Center drop swaps panes; an edge drop places beside it. Escape or dropping outside
  another pane cancels. Zoomed tabs and one-pane tabs don't drag. The grip appears
  on top-edge hover, regardless of the header's position or presence. Only the grip
  starts dragging; header mods don't need drag handlers. `panes()` exposes `movable`.
- `floating`: blocks over this node (below).
- `window_drag: true`: dragging the node moves the window, a double-click
  maximizes it. A mod's own control inside opts out by stopping the press:
  `.on_mouse_down("left", (_e, cx) => cx.stop_propagation())`.
- Root only: `"titlebar": "none"` hides the system title bar so the layout
  draws its own (a bar mod in a `window_drag` block). macOS keeps its window
  buttons over the layout at `"traffic_lights": [x, y]` (px); leave them
  room. Where the system draws no buttons (`window().buttons`, Linux and
  Windows), the bar draws them with `window.minimize`, `window.maximize`, `window.close`.
  Windows' shipped tab bar and system strip use vector window icons, with a
  restore icon when maximized, rather than font glyphs.
- Root only: `"preview": "layouts/<name>.svg"` (or png): the picture the
  settings screen shows for this layout. Draw one when you make a layout;
  without it the person sees a plain frame. New layouts and `themes/*.json`
  show up in settings by themselves.
- Root only: `"terminal_menu": "my-menu"` chooses the mod for terminal
  right-clicks (including Shift+right-click). Omit it for the shipped `menu`;
  an unknown mod logs a warning and falls back to `menu`. Clone a shipped
  layout before changing this field. Other menus are unchanged.
- Colors are theme names (`border`, `accent`, `surface`, ...) or `#rrggbb[aa]`.

A personal terminal menu replaces the whole menu; it doesn't merge items.
Create `mods/my-menu/main.js` and `mod.json` (e.g.
`{"place":"none","overlay":{"width":280,"height":100}}`), then set the layout root's
`"terminal_menu": "my-menu"`. It opens as a pointer popup, default 220×260 px;
its `mod.json` overlay width/height override those defaults. It gets
`props()` = `{tab: "pane", target: {pane, terminal}, overlay}`. Always use
`target.terminal` or `target.pane` in actions, not the focused terminal:
right-clicking a different terminal must act on the one clicked. A terminal
shown outside a tab (a layout terminal block, a window) has no pane:
`target.pane` is its block's id, not in `panes()`, so leave out pane items
there, as the shipped menu does. For example:

```js
import { View, div } from "gpui-kit";
import { props, run, closeOverlay } from "herdr";
export default class MyMenu extends View {
  render() {
    return div().id("split-target").child("Split this pane").on_click(() => {
      const {target, overlay} = props();
      closeOverlay(overlay);
      run("pane.split", {pane: target.pane, direction: "right"});
    });
  }
}
```

### The look: on the layout, cascading

A layout's own look wins; settings.json fills what it leaves unset.
The theme picker overrides both. These go
on any node, and the nearest node up the tree wins, so the root sets the
layout's look and a block can differ:

```json
{ "id": "root", "type": "row", "theme": "graphite", "colors": { "accent": "#a7b4c6" },
  "font": "Geist Mono", "font_size": 15, "ui_font": "Geist",
  "terminal_padding": [10, 6], "terminal_opacity": 1, ... }
```

- `theme`: `graphite`, `ink`, `moss` (dark), `paper` (light), or the name
  of a file in `themes/`; or one for each appearance:
  `"theme": { "light": "paper", "dark": "graphite" }` follows the system's
  light or dark mode (or the person's `appearance`). `app.theme {name}`
  sets the half of the name's lightness. A layout without a theme shows
  settings.json's theme, else graphite or paper. `colors` overrides any theme color by name (see
  `theme()` below), for terminals and panels alike: overrides apply in
  both appearances, so name theme colors (`chrome`, `canvas`) rather than
  writing hex when a layout should work light and dark.
- `themes/<name>.json`: `{ "background", "foreground", "selection",
  "cursor"?, "ansi": [16 colors], "colors"?: {...} }`, all `#rrggbb`; panel
  colors are derived from these, `colors` overrides them. Captain ships
  `atom-one-dark`. The original unedited Atom One Dark and Win98 theme files
  are upgraded to readable terminal colors; edited theme files are kept.
  A theme named like a layout (`themes/win98.json`) is
  that layout's own: Settings offers it only there.
- `font` / `font_size`: the terminals' (px at zoom 1); `ui_font`: the mods'
  (their text sizes are their own, in px, and follow zoom). Optional: without
  them a layout uses settings.json's, else Captain's (JetBrains Mono 14, Geist).
  Captain (main) leaves these unset; Terminals sets text size 15.
  JetBrains Mono, Geist Mono and Geist ship with Captain; any installed font
  works (a terminal font the machine lacks falls back to JetBrains Mono).
  A layout brings its own fonts: `.ttf`/`.otf` files in a mod's `fonts/`
  folder (or the home's `fonts/`) load at start and on the next look change,
  so a layout and its mod folder travel together.
- `pane_scrollbars` in settings.json (default `true`): show terminal scrollback
  with a slim thumb and track. Drag the thumb or click the track to jump there.
  With little or no terminal padding, the hit lane overlays the last 8px;
  terminal columns do not change. Hidden in alternate-screen programs.
- `terminal_opacity` (0..1): below 1, the background behind a terminal
  (a pane frame's, the layout's gradient) shows through its text.
- Switching layouts switches the look. The theme menu changes the shown
  layout's root `theme`.
- Today only the root's look applies; a block's own look is coming.

### Pane frames

The tab block says how each pane of the shown tab is wrapped:

```json
{ "tab": "active",
  "pane": { "gap": 6, "radius": 6, "background": "surface",
            "border": { "width": 1, "color": "border" }, "focus": { "width": 1, "color": "accent" },
            "header": { "content": { "mod": "pane" }, "height": 30, "position": "top", "reveal": "always" },
            "lone": false } }
```

- The look applies to every pane; `focus` replaces the border while the
  pane has the keyboard; `gap` is the space between panes.
- `header`: a mod drawn on each pane, one copy per pane, with
  `props().pane` and `props().terminal`. It reads its live facts from
  `panes()` (title, folder, branch, agent status, focused, zoomed) and its
  buttons run pane commands with that pane (`pane.split`, `pane.zoom`,
  `pane.close`). `position`: `top` or `bottom`; `reveal: "hover"` shows it
  only while the pointer is on the pane, drawn over the terminal so the
  terminal never resizes. The shipped `pane` mod is an example header.
- `lone: true` frames a tab's only pane too (default: only when split).
- Each layout has its own, so one can stay bare while another makes
  every pane a card.

### Floating blocks

```json
"floating": [ { "id": "board", "content": { "mod": "board" }, "edge": "left", "align": "center", "peek": 52 } ]
```

A floating block sits over its node, `edge` (`top`, `right`, `bottom`,
`left`) and `align` along it (`start`, `center`, `end`), 12 px in. It sizes
to its content (render nothing to hide it). People drag it anywhere; on
release it springs to the nearest edge and third, and Captain saves the new
spot. Clicks and hovers on it never reach what's underneath.

`peek` (side edges only) makes it a drawer: only that many px next to the
edge show until it's hovered, then the rest slides out; it slides back a
moment after the pointer leaves. The mod draws one full layout and never
changes it on hover (that's what keeps it smooth): put what should peek
(status, icons) on the edge side of each row.

Captain draws the frame around a floating mod (theme background, border,
rounded corners, shadow; accent border while dragged), like a dialog's,
with 5 px above and below. The mod draws only its content, no panel: on a
side, rows can run edge to edge. It reads `placement()`:
`{ edge, align, vertical, hovered, dragging }`. Lay out vertically when
`vertical` (left/right edges), horizontally otherwise; on the right edge,
mirror rows so their edge side is on the right.

The default window:

```json
{ "id": "root", "type": "row", "divider": { "width": 1, "color": "border" },
  "children": [
    { "id": "sidebar", "type": "column", "size": { "px": 240 }, "min": 40, "max": 480,
      "divider": { "width": 1, "color": "border" },
      "children": [
        { "id": "spaces", "type": "block", "content": { "mod": "spaces" } },
        { "id": "agents", "type": "block", "content": { "mod": "agents" } } ] },
    { "id": "work", "type": "column",
      "children": [
        { "id": "tabs", "type": "block", "size": { "px": 30 }, "content": { "mod": "tabs" } },
        { "id": "main", "type": "block", "content": { "tab": "active",
          "pane": { "gap": 6, "radius": 6, "border": { "width": 1, "color": "border" },
                    "focus": { "width": 1, "color": "accent" } } } } ] } ] }
```

Sidebar on the right: swap the two children of `root`. No sidebar: set
`"hidden": true` on `sidebar` (the user can toggle it: `ctrl-shift-b`).
Narrower than 160px, the sidebar mods draw a compact rail; folding resizes
`sidebar` to 44 (76 on macOS, room for the traffic lights). On macOS,
the tabs also leave room for the window buttons when the sidebar is narrower.

### Layouts and mods other people made

People share layouts and mods as folders on GitHub. Each
is a folder laid out like this home: a layout has `layouts/<name>.json` and
the `mods/<name>/` it needs; a mod is a piece for the person's own window,
so it has only `mods/<name>/` and maybe a `keys.json` with the keys it
expects. Both have a `RECIPE.md` (the brief to build it from scratch) and a
`preview.png`. The files are one build of the recipe. Asked to add one
(a folder on GitHub, or one the person sends):

1. Read the recipe and every file before writing anything. Before adding,
   tell the person if a mod reaches the network, runs programs (`exec`,
   `hc`) or reads files outside its own folder.
2. Copy the layout file to `layouts/` and each mod folder to `mods/`. Leave
   the recipe and preview out. Never overwrite: if a name is taken (yours
   or Captain's), rename the new one: its file or folder, the `mod` names in
   the layout, and the mod's own `mods/<name>/` paths.
3. Fit it to this setup only where its assumptions don't hold (a missing
   program, a name clash) or the person asks. If the files don't work on
   this version, or the person wants it different, build it from the
   recipe instead.
4. A mod's `keys.json`: merge it into the person's. Add its sections and
   keys, and ask before replacing a key they already use.
5. `captain-app check` each mod; for a layout, then `layout.switch {name}`.
   The person's own layout stays as it was; `ctrl-shift-l` switches back,
   and deleting the files removes it.

To share one: put that folder in a public GitHub repo (one folder each if
there are several), write its `RECIPE.md`, save a screenshot of it as
`preview.png` and add the repo topic `captain-layout` for a layout or
`captain-mod` for a mod.

A `RECIPE.md` is a brief: another agent with an empty folder builds the
layout or mod from it. Write it from the real files, in this order:

1. `# Name`, one paragraph of what the person sees, `![Name](preview.png)`.
2. **What you see:** the regions top to bottom or left to right, their
   sizes, colors and type; what each one shows.
3. **How it behaves:** every agent status (working, blocked, done, idle)
   and how it looks; what clicks, keys and hovers do; what moves.
4. **Built from:** the layout's node tree and blocks, each mod and its
   `props` roles, the shipped mods it uses, the `herdr` functions and
   commands it calls, any programs it runs, its art.
5. **Watch out:** what tripped the build up and what a fresh build would
   get wrong; limits (sizes, counts); controls that do nothing yet; what
   it assumes about the setup.
6. **Done when:** checks that prove a build works, each one something the
   build really does.

The person and what they see come first; code only in Built from and
Watch out. Exact over vague (`#ab7748`, 29px, `agent.focus`), and nothing
the build doesn't do.

## Keys: `keys.json`

On upgrade, an untouched keys file from an earlier release gets the current
shortcuts. Any edit, even whitespace, keeps the entire file as yours; new default
shortcuts must then be added by hand.

```json
{
  "global":   { "ctrl-shift-t": "tab.new", "ctrl-shift-o": "pane.split:right", "alt-1": "space.select:1" },
  "terminal": { "ctrl-shift-c": "terminal.copy", "ctrl-left": "send:\u001bb", "ctrl-1": "terminal" },
  "layout:terminals": { "ctrl-shift-pagedown": "space.next", "ctrl-shift-pageup": "space.prev" },
  "mod:editor": { "ctrl-s": "editor.save" }
}
```

- Sections: `global`, `layout:<name>` (while `layouts/<name>.json` shows),
  `terminal` (a terminal has focus), `mod:<name>`. The more specific section
  wins: `layout:terminals` over `global`, `terminal` over both.
- An action is a command (`name`, or `name:value`: the value fills the
  command's first argument), `send:<text>` (types into the terminal),
  `<mod>.<command>` (delivered to that mod's `nextCommand()`), or `terminal`
  (the program gets the key).
- Named, typed command arguments use an object instead of a string:
  `"ctrl-shift-r": {"command": "layout.resize", "args": {"id": "sidebar", "px": 240}}`.
  `command` names an app command; `args` is an object (optional, defaults to `{}`).
  It can pass several arguments, numbers, booleans, arrays or nested objects.
  String actions keep their literal first-argument shorthand, including numeric
  indexes: `space.new:{work}` names a space `{work}`, and `space.new:{}` names it `{}`.
  Invalid binding objects or rejected arguments show an error toast when pressed;
  the key does not go to the terminal program.
- Plain ctrl+letters go to the program unless bound. Programs rely on
  ctrl+c, ctrl+d, ctrl+l, ctrl+r, ctrl+w, ctrl+a, ctrl+e, ctrl+u, ctrl+k,
  ctrl+z and ctrl+[ (Escape); ctrl+alt is AltGr on Windows; option+digits
  and letters type characters on many Mac keyboards (`[`, `@`). Don't pick
  these on your own. If the person asks for one, bind it and tell them what
  it takes from typing.
- Windows AltGr and dead-key compositions type text in terminals. macOS
  dead keys compose accents through native text input; Option keeps its
  keyboard-layout characters rather than acting as Alt.
- On macOS, shifted punctuation shortcuts accept both spellings from the
  current layout: `cmd-shift-]` and `cmd-}` match the same US-layout keypress.
  Existing `keys.json` files don't need to change.
- Terminal keys honor the program's negotiated Kitty mode, including on Windows.
- Windows terminal Escape stays separate from the following key.
- Search: `ctrl-shift-f` (Cmd-F on macOS) opens a find bar inside the focused terminal;
  Enter/Shift-Enter moves through matches and Escape closes it while the find field
  has focus. Clicking the terminal puts typing, including Escape, back in the program;
  the bar and its matches stay.
  Each terminal keeps its own query, and reopening selects it so typing replaces it. Search covers the screen and scrollback without replacing text selection.

## Commands

The one way to change anything; keys, mods (`run(name, args)`), menus and
you use the same list. `captain-app commands` prints every one with its arguments
(which are required) and what it does. Unknown argument names are errors and list
valid names; they never fall back to the current target. For example, closing a
space uses `captain-app do space.close --id <id> --confirmed true`, not `--space`.
Target IDs (`id`, `pane`, `space`, `terminal`) must be strings. Other non-null
types are rejected before a current-target default applies; null means omitted.
The main ones:

- Spaces: `space.new {name?, folder?, tab?, argv?, task?, machine?}`
  (machine: another machine from `hc machine ls`; omitted: the focused space's
  machine for keys and clicks, this computer for scripts; `""`: this computer.
  The space lives there, its
  terminals start in that machine's home unless folder, and its tabs and
  splits stay there; `spaces()` gives each space's `machine`, null here),
  `space.close {id?, confirmed?}`, `space.rename {id?, name}` ("": Captain's name again),
  `space.move {id, index}`, `space.select {index | id}`, `space.next`, `space.prev`.
  With no `argv`, the target hc chooses that machine's default shell (PowerShell 7 when available on Windows).
- Tabs: `tab.new {name?, space?, folder?, argv?, task?}` (argv: an
  agent to run, task: its first message). Agents run in your interactive
  shell (including fish), which stays open after the agent exits. A terminal
  that exits with a nonzero code within two seconds of launch shows an error
  toast with its exit code and last output lines. Or `tab.new {name?, space?, mod,
  props?}` for a tab whose only pane is a mod (a file, a board; split a shell
  in beside it like any tab), `tab.close {id?, confirmed?}` (closing the last tab
  closes its space and asks first unless confirmed),
  `tab.rename {id?, name}` ("": its number again), `tab.move {id, index}`,
  `tab.select {index | id}`, `tab.next`, `tab.prev`,
  `tab.settle {id?}`, `tab.unsettle {id?}` (index, next and prev
  skip settled tabs).
- Panes: `pane.split {direction: "right" | "down", pane?, argv? | mod?, props?}` (a new
  terminal, or a mod pane), `pane.close {pane?}`,
  `pane.zoom {pane?}`, `pane.focus {pane | terminal}`, `pane.go {direction}`,
  `pane.swap {pane?, direction | target}`.
  `pane.go` prefers the most recently focused pane along the nearest edge in that
  direction; unvisited panes fall back to overlap, then top/left layout order.
  `pane.go` also reaches visible mods whose `mod.json` says `"focusable": true`, and
  starts from that mod while it holds the keyboard.
  `pane.swap {direction}` uses geometry only. Focus history lasts until the window closes.
  `pane.swap {target}` trades the places of `pane` (default: the focused one) and
  `target`, another pane of the same tab; give `direction` or `target`, not both.
  A swap moves panes, not focus: the focused pane stays focused in its new place.
  `pane.focus {terminal}` prefers the first visible layout terminal block showing that
  address, then its model pane. Clicking a terminal block focuses it too.
  While a layout terminal block is focused, implicit split/close/zoom/go/swap and
  `terminal.show` fail, as do `terminal.open`/`terminal.keep` with place right/down.
  They never act on the terminal's source pane. An explicit `pane` still targets that
  model pane; composition edits belong to the mod (edit its layout instead).
- Terminals: `terminal.open {argv?, folder?, place?, name?}` (`folder` sets the
  starting directory for tabs and right/down splits, with or without `argv`), `terminal.kill {terminal}`,
  `terminal.keep {terminal, place?}`, `terminal.park {terminal}`,
  `terminal.peek {terminal}` (peek over the tab, with keep-as-tab and close),
  `terminal.show {terminal?, pane?}`, `terminal.search` (screen and scrollback;
  find bar state stays with that terminal), `terminal.copy`, `terminal.paste` (copied
  files, such as Finder's or a clipboard manager's image, as if dropped; an image
  alone as `terminal.paste_image` pastes it), `terminal.paste_image` (Ctrl+V: an
  image, into a terminal on another machine: copied there, its path pasted; on
  Windows, clipboard text is pasted; otherwise Ctrl+V goes to the program, which
  reads the clipboard itself). TIFF and BMP clipboard
  images are converted to PNG before sending,
  `terminal.right_click {to: "program" | "menu", terminal?}` (where right-clicks go
  when the program takes the mouse; kept as its label `herdr.right-click=program`;
  Shift+right-click is always Captain's menu),
  `terminal.scroll {lines | pages | to: "top" | "bottom", terminal?}` (moves Captain's view of the
  history, not the program: negative lines or pages go up, toward history; pages may be
  fractional, one page is the terminal's rows; on the alternate screen, such as vim or less,
  there is no history, so nothing moves),
  `terminal.pin {terminal, pinned}` (pins an agent to the top of the agent lists;
  kept on the terminal as `herdr.pinned`, so every Captain client shows it, and
  gone when the terminal ends; read it as `agents()` `pinned`). Copy, paste, paste-image and implicit
  right-click targeting follow keyboard focus, including peek and floating terminals;
  with no terminal holding keyboard focus, they use the focused pane or block.
  Dragging a text selection past the terminal's top or bottom scrolls while held;
  releasing stops it. Programs that track the mouse still get their mouse events
  (hold Shift to select text instead). A drag stays with the terminal where it
  started, including its release outside the pane; other panes don't receive it.
  Terminals display Kitty graphics, including Unicode placeholder images used by
  programs such as yazi; partial images and scrolling follow the placeholder cells.
- Agents: `agent.next`, `agent.prev`, `agent.focus {terminal}`.
- Layout: `layout.switch {name?}`, `layout.toggle {id}`, `layout.show {id}`, `layout.hide {id}`,
  `layout.resize {id, px | weight}`, `layout.select {stack, child}`, `layout.set {layout}`
  (personal tree edits; on shipped layouts only click/look fields change),
  `layout.reset {name?}` (reset a shipped layout and its clicks),
  `layout.clone {name, as?}` (duplicate, then switch; default `<name>-copy`).
- App: `mod.clone {name, as?}` (duplicate; default `<name>-copy`), `mod.open {name}` (again: closes it), `overlay.close {id?}`, `app.help` (again: closes it), `app.palette` (again: closes it), `app.settings {section?}`
  (sections: `look`, `machines`, `structure`, `alerts`, `updates`, `privacy`; Machines lists, updates, renames and removes ssh machines; Project Structure sets the workspace's scope levels, their words and how they nest; settings edit the same files you do).
  `app.check_updates` (only a successful check reports Up to date; a failed check clears the checked time and can be retried), `app.update_core` (a newer hc of this generation, terminals kept;
  never one a package manager owns), `app.welcome`, `app.whats_new`, `app.feedback {message?, from?}` (from `"person"` or `"agent"`; opens a review window, where the person can paste up to 3 screenshots; never sends until the person clicks Send), `app.reset`,
  `app.theme {name}`, `app.zoom_in`, `app.zoom_out`, `app.zoom_reset`,
  `app.update` (restart into a downloaded update).
  Add machine lists all named ssh-config hosts in a scrollable list. Setup
  prompts open above the picker; close the setup terminal to return to the
  updated list and any machine-specific guidance. If a remote machine was rebuilt
  or replaced and a different hc answers there, its sidebar offers Connect:
  explicitly forget the old identity and pair again with the same name and target.
  Other unreachable machines don't offer this.
- Window: `window.minimize`, `window.maximize` (or back), `window.close`.
- macOS: `app.quit`, `app.hide`, `app.hide_others`, `app.show_all`.

When the user runs a command without a required name (a key, a click), Captain
asks them. **You never get a dialog**: pass every required argument, or the
command says what's missing.

From a shell:

```sh
captain-app do tab.new --name "fix login" --argv '["claude"]' --task "look at the login bug"
captain-app do pane.split:right
captain-app ls          # spaces, their tabs and terminals (sub-agents under their parent)
captain-app status      # what's on screen: layout, focus, overlays, zoom, errors from files and mods
captain-app commands    # every command and its arguments
```

`captain-app do` prints what the command made (`{ "space", "tab",
"terminal" }`, `{ "pane", "terminal" }`) or `{ "error": "..." }` and exits 1.

## The person: `settings.json`

What's about the user, never a look:

```json
{ "zoom": 1, "sounds": { "enabled": true } }
```

- `zoom` scales the whole app: terminals, mods, the layout's px sizes
  (0.5 to 3; the zoom keys change it). Write sizes as designed at zoom 1.
- `sounds`: `{ "enabled": true, "done": "<file>", "blocked": "<file>" }`.
  When an agent needs the person, or finishes while they aren't looking at
  it, Captain plays a sound and (unless they're looking) posts a toast that
  goes there when clicked. `done` / `blocked` replace the bundled MP3s
  (path absolute or relative to `~/.config/captain-app`). Changes apply live,
  including the Play buttons in Settings > Alerts.
- `core`: the hc binary to use, when it isn't where Captain looks
  (PATH, the login shell's PATH, `~/.local/bin`, Homebrew, mise, Nix,
  cargo). Read at start.
- `cst`: a Captain Search `cst` to run instead of the one packaged in Captain.app,
  for development (default: the packaged one; a build without one uses `cst`
  on PATH).
- `font`, `font_size`, `ui_font`: used where the layout doesn't set its
  own. Captain (main) leaves these unset. Bigger panels: `zoom`.
- `font_ligatures`: programming ligatures are off by default; set `true` to
  enable them in terminal fonts such as JetBrains Mono and Fira Code. The cursor
  and background changes (including selections) break ligatures so individual cells stay clear.
- `theme`: a theme name or `{light, dark}`; used where the layout sets none.
  The theme picker overrides any layout and stays on that layout.
- `colors`: theme color overrides (`{"accent":"#ff8800"}`), with the same
  precedence as fonts.
- `appearance`: `"light"` or `"dark"` always; absent, the system's. A layout
  with a light and a dark theme shows the matching one.
- Updates: an installed app downloads them by itself and offers Restart and
  What changed. If the app crashes or is killed with a verified newer update
  waiting, the next launch installs it before opening a window. The first
  start after an app update also updates hc
  to the latest release of the same generation, keeping terminals running.
  Settings → Updates keeps app and hc actions together;
  its What's new button opens the waiting update's release range, or this
  version's notes, in the What's new window. Reading never clears the waiting-update dot.
  Rare spotlight releases open What’s new after restart, once; otherwise a
  quiet Updated pill lasts for that launch. A build from source never updates.
  Curated notes come from the bundled root `CHANGELOG.md`; remote notes and
  media are optional. `update-notes.json` records the last successful app
  version and presented spotlights; `update-notes-cache.md` caches remote
  notes; `update-notes-media/` caches HTTPS images (including animated WebP clips),
  referenced below its item in the changelog as `![label](https://...)`.
  Each version uses `### New` with `- **Headline.** detail` highlights and
  `### Fixed` with short bullet items. Unsectioned bullets are plain items.
  Notes expose `items` with `headline`, `detail`, `fixed`, and cached `media`
  objects (`path`, pixel `width`, pixel `height`). Highlights use matching cards:
  edge-to-edge media at its original aspect ratio, then headline and detail.
  Mods use `updates().notes` and `presentUpdateNotes([version])` only
  for spotlight entries actually shown expanded, not collapsed headlines.
  macOS releases require Apple Silicon; Intel Macs must build from source.
  Keep signed macOS bundles unchanged: don't edit their Info.plist or
  re-sign them. On launch, an installed macOS bundle creates or repairs its
  own `~/.local/bin/captain-app` symlink, leaving personal files and other
  apps' links alone. Disk-image and translocated launches don't create it.
  Finder launches load the login shell's PATH, with the usual install
  folders as a fallback.
- `option_as_alt`: macOS only, `"auto"` (default), `"on"`, `"off"`, `"left"`, or `"right"`.
  Available in Settings → Keyboard.
  `"auto"` sends Option as Alt on US, US International, ABC and ABC-India;
  other layouts keep native Option text and dead keys. `"on"` enables terminal
  Alt shortcuts (including Option+E without composition); `"off"` keeps native
  Option input on every layout. `"left"` / `"right"` makes only that Option
  key act as Alt; the other types characters. Changes apply immediately, including layout
  switches. Linux and Windows are unchanged.
- `minimum_contrast`: terminal text contrast ratio, `1` (default) leaves
  program colors unchanged. Set it to `1.6` to enable the previous readability
  adjustment, or another number from `1` to `21`. Text below that ratio is
  replaced with near-white or near-black, whichever contrasts more with its
  background; this is a threshold, not a guaranteed resulting ratio.
  Box/block, Powerline and sprite glyph colors are never adjusted.
  Changes apply immediately, including text already on screen.
- `clipboard_write`: `false` blocks terminal programs from writing the local
  clipboard (default `true`, including remote terminals attached in this window).
  OSC 52 accepts base64 UTF-8 text up to 1 MiB: `c` writes the system clipboard,
  `p` writes Linux primary selection. `s` is ignored; cut buffers `0`–`7`
  are treated as the system clipboard.
  Clipboard reads (`?`) are never allowed.
- `pane_scrollbars`: `false` hides the scroll position thumb at a terminal
  pane's right edge (default `true`). It shows only when the pane has
  scrollback — never on the alternate screen (editors, pagers) — and
  drags to move through it.
- `telemetry`: `false` stops anonymous usage data and deletes what wasn't
  sent (on when absent; Settings → Privacy shows each report word for
  word, and `telemetry()` in mods). Once a day the app sends counts:
  version, OS, which known agents ran, how many spaces, tabs and
  terminals, which layouts, shipped mods and commands were used. Never
  terminal content, names of files, folders, spaces or the person's own
  mods; nothing from herdr-core. `DO_NOT_TRACK=1` or `HERDR_TELEMETRY=0`
  turns it off for one run. A build from source never sends.

## Programs: `programs.json`

Every pane, agent and terminal carries `program`: its name, icon and tile,
from one table, so every mod shows the same, and the app's own toasts
("Kiro finished") use the same names. An agent's own report wins;
else the program the terminal runs (an interpreter shows what it runs:
`node .../vite` is Vite, `npx vitest` is Vitest); shells are `shell`,
anything unlisted is `program` with a color of its own. The built-in table
has 120 programs (agents, editors, runtimes, tools, databases, infra);
`programs.json` here adds to it or changes entries field by field:

```json
{ "programs": {
    "lazygit": { "name": "Git", "tile": "#f05032" },
    "mytool": { "name": "My Tool", "icon": "my-icons/mytool.svg", "tint": true,
                "tile": "#334155", "color": "#ffffff", "match": ["mytool-dev"] } },
  "shells": ["xonsh"], "interpreters": ["pipx"] }
```

- `icon`: a path in the home folder. `tint: true` for a one-color glyph
  (drawn in `color` on a tile, or the text color), false for a picture.
- `match`: other process names (or agents' `app` names) for the entry.
- `run`: how to start it (`["cursor-agent"]`), for agents people start
  from a menu: `knownAgents()` lists the entries with one, and whether this
  machine (or another) has them installed. For example, override
  `"claude": { "run": ["my-claude"] }` or add
  `"claude-alt": { "name": "Claude (alt)", "icon": "icons/claude.svg", "run": ["my-claude-alt"] }`
  inside `programs`. Use programs or shell functions, not aliases (the launcher
  does not expand them). On macOS the menu launcher loads login profiles before
  running the agent and leaves an interactive login shell when it ends.
  Codex starts as `codex --no-daemon`: a shared Codex background process would make later
  sessions use the first terminal's identity and stop with it.
  On remote Unix machines, both the installed-agent check and launcher load
  that user's login/interactive shell for sh, bash, zsh, dash, ksh and fish, so
  its PATH includes shell-configured tools. Other shells keep the sh launcher.
  Local Linux launches keep interactive non-login shells. Local and remote checks
  include functions in bash and zsh; fish checks find executables only. Installation
  checks are cached for up to a minute; changing the commands checks again.
  Invalid JSON is reported in `programs.error` (a valid file has no error).
- It follows what runs in the terminal now (lazygit, nvim), else what it
  started with.

## Mods: `mods/<name>/`

```
~/.config/captain-app/mods/<name>/
  main.js       the mod (required)
  mod.json    where it first appears: { "place": "right" | "left" | "top" | "bottom" | "none", "size": 320 }
  *.js, *.svg   more modules ("./ui.js"), its own icons
```

- `"focusable": true` in `mod.json` lets `pane.go` give a shown copy the keyboard.
  It gets `"focus"` from `nextCommand()`: focus your own handle then
  (`this.handle.focus()`) and handle keys with `on_key_down`. The copy gets `"focus"`
  again after a popup over it closes. Leave with `run("pane.go", {direction})`.
  Without this opt-in, navigation and clicks stay unchanged.

- The first time Captain sees a folder, it docks one copy per `mod.json` into
  the shown layout (`"none"`: only where a layout or another mod puts it).
  After that, the layouts decide. Saving any file reloads every copy in place
  (not files the app writes: `jsconfig.json`, `.d.ts`, hidden ones,
  `node_modules`). The old build's tasks and timers stop: a loop ends at its
  next `cx` use. A broken save keeps the old version running and shows in
  `captain-app status`; its top-level code has already run, so keep side effects
  on `globalThis` out of the module's top level.
- A copy starts the first time it shows, then stays alive while its block is in
  the layout, shown or not (a hidden column, the other child of a stack, a pane
  in another tab or behind a zoom): switching back finds it as it was. Its
  timers keep running while hidden, so poll gently. Another layout starts its
  mods fresh (pane mods belong to their tab and stay); a block removed or given
  another mod or props starts over. `<mod>.<command>` reaches shown copies only.
- Images and SVGs are referenced from `~/.config/captain-app`:
  `image("mods/<name>/logo.svg")` (full color), `svg("mods/<name>/icon.svg")`
  (tinted with the text color). Saving an asset under `mods/` updates every
  mod showing it, including images that were missing or invalid on first load.
- The defaults are mods too, read them as examples: `spaces`, `agents`,
  `tabs` (the tab bar), `menu`, `prompt`, `confirm`, `help`, `toast`,
  Terminals' `picker`, `peek`, and `pane` (an
  example pane header, not used by the default layouts), and `worktree`
  (git worktrees as spaces: the space menu's and the + menu's New worktree,
  Open worktree (the repo's checkouts, filtered; one already shown is gone
  to) and Remove worktree open it as a dialog with props `{space, folder?,
  action: "new" | "open" | "remove"}` (the repo: folder, else where the
  space's first terminal is), or with `{branch, tab?, argv?}` to make one at once with
  that first tab; checkouts go to
  `~/.captain/worktrees/<repo>/<branch>` on the space's machine, a constant
  at its top; the
  `spaces` sidebar nests worktree spaces under their repo's and shows each
  space's repo, branch, ahead/behind and uncommitted changes). Shipped mods
  are Captain's; duplicate with `mod.clone` before editing.

### Check before you finish

```sh
captain-app check ~/.config/captain-app/mods/<name>
```

Loads the mod, renders it once, exits nonzero with the exact error. Run it
after every change. Never call a mod done without a passing check.

### A mod

```js
import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, spaces, theme, run } from "herdr";

export default class Branches extends View {
  init(_props, cx) {
    this.branch = {};
    cx.timer.every(3000, (cx) => this.refresh(cx));
    this.refresh(cx);
  }

  refresh(cx) {
    cx.spawn(async (cx) => {
      for (const p of spaces()) {
        // on: a space on another machine runs git there.
        const r = await exec("git", ["branch", "--show-current"], { cwd: p.folder, on: p.machine });
        this.branch[p.id] = r.stdout.trim();
      }
      cx.notify();
    });
  }

  render(cx) {
    const t = theme();
    return v_flex().size_full().bg(t.surface).p(10).gap(4).text_size(13).children(
      spaces().map((p) =>
        h_flex().id(`b-${p.id}`).gap(8).hover((s) => s.bg(t.raised))
          .on_click(() => run("space.select", { id: p.id }))
          .child(div().text_color(p.active ? t.foreground : t.muted).child(p.name))
          .child(div().text_color(t.dim).child(this.branch[p.id] ?? ""))));
  }
}
```

Rules:
- `main.js` default-exports a class extending `View`. `init(props, cx)` runs
  once; `render(cx)` describes the UI and runs again after `cx.notify()` and
  whenever Captain's live data changes (spaces, agents, terminals, focus,
  theme, shared state). Don't poll those.
- **Never keep a `cx` and use it later.** Every callback gets its own:
  `on_click((event, cx) => …)`, `cx.spawn(async (cx) => …)`,
  `cx.timer.every(ms, (cx) => …)`.
- No `setTimeout`/`setInterval` (use `cx.timer`), no top-level `await` (use
  `cx.spawn` in `init`). Anything slow goes through `exec` or `fetch`.
- Size: put `.id("root").on_size_change((size, cx) => { this.size = size; cx.notify(); })`
  on the root `div()`, `h_flex()` or `v_flex()`. It reports `{width, height}`
  in logical pixels after the first layout and whenever the size changes,
  even with the pointer elsewhere. It does not change `props()`.
- Cleanup: an optional `async close(cx)` on the exported `View` gets one final
  call when its pane, tab, layout-owned slot or overlay goes, and on app quit.
  It is not called for a hidden copy or an inactive tab. Start cleanup inside
  `close()`; don't await work started earlier. Await cleanup directly,
  for example `async close(cx) { await exec("hc", ["kill", this.browser]); }`.
  Closing stays responsive: cleanup has at most 250 ms (shared across copies
  on quit), then unfinished tasks are cancelled. On quit, use `exec(...)` and
  `run("terminal.kill", { terminal: address })`: those go out before the event
  loop stops; other commands may not run. An `exec` already started keeps
  running; don't start new long-lived work in `close`. This is best-effort,
  not a guarantee after a crash or forced termination.
- Use `theme()` colors so the mod follows the user's theme.
- **Animate natively, never with a timer.** Every redraw of a mod redraws
  the whole window, so `cx.timer.every(90, () => cx.notify())` for a
  spinner costs real battery. Use `.frames(["⠋", "⠙", …], 90)` (shows
  each string in turn as the element's text), `svg(arc).spin(900)` (turns
  an icon once every 900 ms: a ring spinner, e.g.
  `mods/workspace-chrome/spinner.svg`), or `.loop("opacity", { from:
  0.4, to: 1, period: 1200 })` (also width, height, left, top), and
  `.transition(...)` for changes: they run without your script and stop
  when not shown. Timers are for data (a clock, a fetch), and only as often
  as what they show changes.

### UI

Chained builders with GPUI's snake_case names: `div()`, `h_flex()`,
`v_flex()`; `.size_full()`, `.flex_1()`, `.gap(n)`, `.p(n)`, `.px(n)`,
`.w(n)`, `.h(n)`, `.items_center()`, `.justify_between()`,
`.overflow_y_scrollbar()`; `.child("text")`, `.text_size(n)`, `.font_semibold()`,
`.truncate()`, `.text_color(c)`; `.bg(c)`, `.border(1)`, `.border_color(c)`,
`.rounded(n)`; `.id("unique")` then `.on_click(...)`, `.on_mouse_down("right", ...)`,
`.hover((s) => s.bg(c))`, `.on_scroll_wheel(...)`; keyboard: `this.h = cx.focus_handle()`
in init, `.track_focus(this.h).on_key_down((e, cx) => …)`, `this.h.focus()`.
`.on_drop((event, cx) => …)` on a plain layout element receives files and folders
dragged in from Finder as `event.paths` (absolute paths), and `.drag_over((s) => s.bg(c))`
styles it while they're over it, so a drop target can show where they'll land. The feedback form accepts dropped images with
the same limits, previews and removal as picked images.
`.on_hover((hovered, cx) => …)` reports pointer entry and exit on buttons
and other interactive controls too.
`.when(cond, (el) => …)`, `.children([...])`. Controls from `gpui-base`
(`Button`, `Checkbox`, `Switch`, `InputState` + `Input`) are unstyled: style
them. Text: `TextView.markdown(id, text)` renders markdown; `EditorState.new({
language, value, readonly, font_family: settings().font_family, syntax: {
keyword: theme().ansi5, ... } })` + `Editor.new(state)` is a code view (readonly)
or editor with syntax highlighting, line numbers and folding. A viewer opens
beside the shell as a pane: `pane.split {direction, mod, props: { file }}`.
The full surface is in `gpui-kit.d.ts` next to this file.

### The `herdr` module

All of it is in `herdr.d.ts`. In short:

- Live data: `spaces()` (each with its tabs, status, name, named, folder,
  machine, branch), `machines()` (name, null for this computer; layout:
  `loading`, `ready`, or `unavailable` on this connection; loading ends on
  the first read even if it fails, and offline machines are unavailable),
  `agents()` (status, app, space, tab, pinned, focused; "done" turns "idle"
  once someone looks at it on any Captain client), `terminals()`
  (every hc terminal), `focused()`, `layout()`, `theme()` (every color by
  name: background (the terminals', cards), canvas (behind cards), chrome
  (sidebars, bars), surface, popover, raised, hover, selected, border,
  input (a stronger border), foreground, muted, dim, accent,
  accent_foreground, solid and solid_foreground (a primary button),
  selection, working, blocked, done, idle, unknown, ansi0..15; light and
  dark themes name the same ones), `settings()` (with `dark`: the
  appearance now), `keys()`, `commands()` (every app
  command, its arguments and what it does), `layouts()` (the layout names).
  `focused().agent` is the agent an agent block shows.
  `focused().space`/`tab` describe the current window context. With a layout terminal
  block focused, `pane` and `shown` are null; `terminal` and `cwd` describe that block,
  not its source pane. A terminal in a window (`openOverlay` with a `layout`) that has
  the keyboard is reported the same way; pane commands without a `pane` still act on
  the focused pane under the window. Check `focused()?.pane` before offering pane controls.
  `panes()` still lists only model panes; none is focused while a terminal block is.
  For composition navigation: `run("pane.focus", {terminal: nextAddress})`.
  Terminal copy/paste and implicit right-click routing follow the focused block.
- Floating: `placement()` (null unless this copy floats).
- Act: `run(command, args)`.
- Overlays: `openOverlay({ mod, props, kind: "dialog" | "popup" | "window", width, height, at, frame })`
  returns an id; the mod gets `props().overlay` and closes itself with
  `closeOverlay(id)`; Esc closes the top dialog or popup. `frame: "none"`
  drops Captain's border, corners and shadow so the mod draws its own.
  `notify({ title, body, level, icon, timeout })` posts a notification, drawn by the `toast` mod.
- Windows (`kind: "window"`): stay open until closed, over the layout and
  under dialogs; a press the mod doesn't stop drags it (stop it on your
  controls, like a title bar's buttons), it stays where dropped, and the one
  pressed last is on top. Sized by content unless `width`/`height`; at `at`
  or centered. `key: "name"` remembers its spot (opening that key again
  replaces it); `keep: true` with a key reopens it on the next start. A
  layout switch closes windows; kept ones come back with their layout.
  A window can hold a layout, terminals too: a terminal keeps its presses
  (selecting text), so give it a mod block to drag by, a title bar:
  `openOverlay({ kind: "window", key: "t7", width: 600, height: 400, layout: { type: "column", children: [{ type: "block", size: { px: 28 }, content: { mod: "my-titlebar" } }, { type: "block", content: { terminal: "t7" } }] } })`.
- Machine: `exec(cmd, args, { cwd, on, timeout })` (the user's real environment; use this,
  not `process.run`; `on: space.machine` runs there, omitted means local).
  `timeout` is milliseconds, defaults to 30,000 and is capped at 600,000 (10 minutes),
  locally and remotely. It rejects on timeout, if the program cannot start or the machine cannot be reached.
  `hc(args)` (`hc read <t>` gives a terminal's text),
  `home()`, `appHome()`, `appBinary()` (this app's own binary, to run `check`), `playSound("done" | "blocked" | file)`.
  `knownAgents({ on })`: the agents people can start (programs.json's
  `run`), most picked first, each `installed` or not there (asked of the
  shell agents start in; a remote Unix machine's login/interactive shell when supported);
  start one with `argv: agent.run`.
  `pickFolder({ prompt })`: the system's folder picker (here only),
  resolves with the folder or null.
- Files: `readTextFile(path, { on })` (here, or on that machine),
  `writeTextFile(path, text, expected)` (here only), and
  `watchFolder(dir)` (here only; poll a remote space's folder): resolves with the changed paths the next time anything
  under `dir` changes. On Linux, Git-ignored directories and Git objects are skipped,
  and trees above 4096 non-ignored directories reject the watch; use a timer fallback.
  macOS and Windows watch recursively, including ignored directories.
  A returned root path means refresh everything (the pending paths are bounded).
  Files also refreshes on its 10-second timer:
  `cx.spawn(async (cx) => { for (;;) { await watchFolder(dir); this.reload(); cx.notify(); } })`.
- `notify({ ..., action: { name, args } })`: the toast runs the command when clicked.
- This copy: `props()`, `shared(key)` / `setShared(key, value)` (every mod
  sees it; setting re-renders all), `nextCommand()` (key commands for this
  mod: `cx.spawn(async (cx) => { for (;;) { const c = await nextCommand(); …; cx.notify(); } })`).

Mods are trusted code: commands and network access (including `fetch`) are unrestricted.
Other built-ins: `localStorage` (per mod copy), `console.log`, `fetch`,
`import * as fs from "fs/promises"` (read in home, write under `~/.config/captain-app`).

## `hc`

`captain-app --skill` prints the manual for driving this window;
`hc --skill` prints the manual for running and coordinating programs and agents,
including a small hc skill you can install with the user's permission.
`captain-app commands` lists app commands and arguments without needing an open window.

- `hc ls --json`, `hc read t3 --lines 50`, `hc run --json --cwd <dir> --label k=v -- <cmd>`,
  `hc label t3 k=v`, `hc send t3 "text"`, `hc wait t3`. `hc --help` for all.
- Other machines: `hc machine ls`; `hc run --on <machine> ...` starts a
  terminal there (`vps/t7`), `hc exec --on <machine> git status` runs one
  command there and prints exactly its output.

## Keep in mind

- A task's `cx` belongs to the copy that started it: `cx.notify()` redraws
  that copy, even when another copy woke it (a shared promise).
- One mod per idea. Don't edit `AGENTS.md` or the `.d.ts` files (the app
  rewrites them). Don't touch other mods unless the request
  needs it.

## Sample North Pole

`cst demo seed` loads the separate `sample-north-pole` workspace: Sample Gift Catalog,
Sample Workshop Supply, Sample Polar Outfitter (50 products each), six Sample lists,
30 search queries and illustrative manual judgments for 10 queries. Prices, stock,
and specifications are synthetic demo data, not verified commercial offers. Unlisted
query/product pairs are unjudged. No clicks, conversions or A/B test results are
supplied. The app's “Load Sample North Pole” task selects this workspace.

Use “Remove Sample North Pole” in the New menu/palette, or `cst demo remove-sample`,
to remove it. Removal refuses added or modified files: move your work out first.
Sample-only read history is removed too. Other workspaces are preserved. `cst --dry-run demo seed` and
`cst --dry-run demo remove-sample` preview without changing sample data.
The older generated demo remains available with `cst demo seed --legacy`;
it writes simulated offline evaluations into the selected workspace.

## Captain project page (Home)

The sidebar's **Home** opens the current project's page (`captain-project`): the Explorer's pick, else the
last project opened there. With no project it offers New project, which can pin a catalog at once.
- **Inputs**: catalog, events, queries, labels and the sealed test set, each pinned to an exact version. A newer
  version shows as information with one action ("Use version 2"); a missing input asks to choose one.
- **Train and compare**: candidates, each one trained version judged against the baseline on the sealed test set
  and the pinned labels. A model trained on the labeler that grades the test set reads "Agrees with its own
  labeler", never as a win. New candidate trains on the pins with your own command; Import a model version brings
  one from MLflow or a registry. Rows open to: the comparison, Rerun with changes, Judge against the baseline,
  Make it the baseline (with its evidence) and Publish to the registry.
- **Ship**: the registry place (asked once), staging (not yet), the launch review, and the online check. The
  baseline is what MLflow and Unity Catalog users call the `champion` alias.
The page links to Workflows, which also opens from the palette ("Open workflows"). The forms are
`captain-forms` tasks (`pin`, `generate-events`, `experiment`, `import-version`, `publish`, `champion`) opened with
`props: {task, values}`; a failed launch criterion and a decision to go on offer their next step.

## Synthetic shopper events

A project with a catalog and no traffic yet can train on generated events. On the project page, an empty events
input offers **Generate synthetic events** once the catalog is set (the `captain-forms` task `generate-events`,
`values: {project, slot}`): sessions and days, then a job writes `<project>/synthetic-events` and sets it as the
input. From a terminal:

```
cst sim events --catalog shop-catalog --name sim/shop-events --sessions 20000 --days 30 --seed 7
cst projects generate-events my-project --sessions 20000 --days 30
```

- One row per impression: `session_id, user_id, ts, device, query, query_class, product_id, position, clicked,
  added_to_cart, purchased, price, simulated`. Queries come from the catalog's own words, pages from a local BM25
  ranker, clicks from position and text match, purchases from the shopper's budget.
- It is all **simulated**. The dataset is tagged `synthetic`, its card shows a "simulated" badge, a trainer gets
  `label_source: simulated`, and a project judged on it reads "Replay of logged searches, graded from clicks" with
  SIMULATED evidence. Never report a number from it as a result on real shoppers.
- It reproduces: the same catalog version, seed, sessions, days and `--end` date give the same rows. The
  provenance records them all, and lineage links the events to the catalog version.
- It refuses a project with no catalog, and a dataset that isn't a catalog.

## Words

The screens use the words search and recommendation teams use. The CLI, the API, the files and the ids keep
their earlier names, so saved workspaces and scripts keep working. Translate with this table: say the screen
word to a person, and type the CLI word.

| On screen | In `cst`, the API and ids | What it is |
|---|---|---|
| Offline evaluation | `backtest` (`cst backtest`, `/api/backtests`, the `backtests` tab id) | Two or more versions scored on the same queries |
| Evaluation settings | the `captain-sandbox` mod, `/api/sandbox` | Other settings of one version, tried offline |
| Candidate | a project `experiment` (`/api/projects/<id>/experiments`, form task `experiment`) | One trained version in a project, judged against the baseline |
| Baseline | `champion` (`/api/projects/<id>/champion`, form task `champion`) | The version a project ships and judges candidates against |
| Experiment | `investigation` (`cst investigate`, `/api/investigations`, `CST_INVESTIGATION`, section id `investigations`) | A hypothesis, its evaluations and a decision |
| Rating, rating queue, rater | `grading`, `grader` (`cst grade`, `/api/grading`) | People giving grades 0 to 3; the data stays "judgments" and "grades" |
| Compare | section id `comparisons`, the `captain-compare` mod | One evaluation run read in depth; a single result is still a comparison |
| Launch review, launch criteria | `handoff`, readiness gates (`cst handoff`, `/api/handoffs`, section id `handoff`) | A candidate checked and passed to your own deployment process |
| Off-policy evaluation | `replay` (`cst replay`, `/api/replays`, the `replays` tab id) | Counterfactual replay of logged traffic |
| Sealed test set | `holdout` (the `holdout` slot, the `:holdout` split, `--holdout`) | Queries no model trains on, kept to judge them |
| Model version | `pipeline` version (`cst pipeline`, `/api/pipelines`, `name@vN`) | A version of a ranker; "search configuration" when it has no trained weights |

The palette still finds the earlier words: typing "backtest" finds "Run an offline evaluation".

## Captain team collaboration

The sidebar's **Team** and the Captain palette's **Team collaboration** entry open the `captain-team` mod.
The editor keeps the selected revision when you pull newer records, so stale drafts
conflict instead of overwriting them. A restored server history clears the editor;
reload and review before saving or resolving a draft.
Connect with a server URL, workspace and masked access token using the protected
local `/api/team/connect` route. Captain checks access and stores credentials
in the OS keychain; the mod clears the token field after each attempt. Company SSO
is optional when configured. The mod provides cached records, explicit pull/push,
local drafts and reviewed conflict resolution. The team workspace is independent
of bucket workspace selection.
New records are private; saving a draft does not publish it. See
`docs/captain/TEAM-SYNC.md` in the source tree for configuration and verification limits.
