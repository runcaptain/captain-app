// A tiny host for a gpui-kit mod in Node (with stubs/register.mjs): a fake cx whose spawned tasks and timers the
// test runs by hand, a render that walks the recorded element tree, virtual lists drawn for a chosen range, and
// clicks by element id.
// The most path operations one draw may build, over everything it paints. The app stops a draw that takes too long
// and shows "This view could not be re-rendered"; a draw that built several thousand shapes did exactly that in a
// real window while every render test passed. Counted on every draw the harness makes.
export const SHAPES_PER_DRAW = 2000;
export function shapesIn(tree) {
  let n = 0;
  (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path') n += e.args?.[0]?.ops?.length ?? 0; for (const k of e.kids ?? []) w(k); })(tree);
  return n;
}

export function harness(Mod) {
  // A test that says nothing about the app's shared values gets services that are up, as a screen normally finds
  // them. A test about starting up sets `globalThis.__shared` itself (to {} for "not up yet") before mounting.
  if (globalThis.__shared === undefined) globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
  const pending = [];
  const timers = [];
  const timings = [];
  const cx = {
    spawn: (f) => pending.push(Promise.resolve().then(() => f(cx))),
    timer: { every: (_ms, f) => timers.push(f), after: (_ms, f) => timers.push(f) },
    notify() {},
    stop_propagation() {},
    focus_handle: () => ({ focus() {} }),
  };
  const mod = new Mod();
  let tree = null;

  // Long-lived tasks (a kernel socket's read loop) never finish: wait for each batch at most 40 ms.
  async function settle() {
    for (let i = 0; i < 50 && pending.length; i++) {
      await Promise.race([Promise.all(pending.splice(0)), new Promise((r) => setTimeout(r, 40))]);
    }
  }
  function render() {
    const t0 = performance.now();
    // The app refuses InputState.new and EditorState.new while drawing (stubs/gpui-base.js throws the same).
    globalThis.__rendering = true;
    try { tree = mod.render(cx); } finally { globalThis.__rendering = false; }
    timings.push(performance.now() - t0);
    const shapes = shapesIn(tree);
    if (shapes > SHAPES_PER_DRAW) throw new Error(`one draw built ${shapes} shapes (the ceiling is ${SHAPES_PER_DRAW}): the app would stop it and show "This view could not be re-rendered"`);
    return tree;
  }
  function walk(e, f, ranges = {}) {
    if (!e || typeof e !== 'object') return;
    f(e);
    if (e.kind === 'v_virtual_list') {
      const [id, count, , , draw] = e.args;
      const [a, b] = ranges[id] ?? [0, Math.min(count, 20)];
      const t0 = performance.now();
      globalThis.__rendering = true;
      let rows;
      globalThis.__inItemRenderer = true;
      try { rows = draw({ start: a, end: Math.min(b, count) }, cx); } finally { globalThis.__rendering = false; globalThis.__inItemRenderer = false; }
      timings.push(performance.now() - t0);
      for (const r of rows) walk(r, f, ranges);
    }
    for (const k of e.kids ?? []) walk(k, f, ranges);
  }
  const all = (ranges) => {
    const out = [];
    walk(tree ?? render(), (e) => out.push(e), ranges);
    return out;
  };
  return {
    mod,
    cx,
    timings,
    asked: () => globalThis.__asked ?? [],
    async start() {
      mod.init(globalThis.__props ?? {}, cx);
      await settle();
      render();
    },
    /** Draw the named lists for these ranges (as layout would), run the timers, fetch, and render again. */
    async layoutLists(ranges) {
      render();
      all(ranges);
      for (let i = 0; i < 6; i++) {
        for (const f of timers) f(cx);
        await settle();
      }
      render();
    },
    text(ranges) {
      const parts = [];
      walk(tree ?? render(), (e) => {
        for (const k of e.kids ?? []) if (typeof k === 'string' || typeof k === 'number') parts.push(String(k));
      }, ranges);
      return parts.join(' | ');
    },
    byIdPrefix(prefix, ranges) {
      return all(ranges).filter((e) => typeof e.call === 'function' && String(e.call('id')?.[0] ?? '').startsWith(prefix));
    },
    has(e, kind) {
      let found = false;
      walk(e, (x) => { if (x.kind === kind) found = true; });
      return found;
    },
    /** Press a key chord ("cmd-enter") on the element with this id: its on_key_down handler. */
    async key(id, keystroke, ranges) {
      const e = all(ranges).find((x) => typeof x.call === 'function' && x.call('id')?.[0] === id);
      if (!e) throw new Error(`no element with id ${id}`);
      const h = e.call('on_key_down')?.[0];
      if (!h) throw new Error(`${id} has no on_key_down`);
      h({ keystroke }, cx);
      await settle();
      render();
    },
    /** Let spawned work run (kernel replies arrive on timers), then render. */
    async idle(ms = 60) {
      await new Promise((r) => setTimeout(r, ms));
      await settle();
      render();
    },
    /** Click item `index` of the virtual list `listId`: the list's on_item_click handler, with that item's key. */
    async itemClick(listId, index, ranges) {
      const list = all(ranges).find((x) => x.kind === 'v_virtual_list' && x.args[0] === listId);
      if (!list) throw new Error(`no list ${listId}`);
      const h = list.call('on_item_click')?.[0];
      if (!h) throw new Error(`${listId} has no on_item_click`);
      h(list.args[3](index), cx);
      await settle();
      render();
    },
    /** Fire a handler registered on the element with this id: on_drop ({paths}), on_mouse_down / on_mouse_up
     *  (registered with a button first: the handler is the last argument). */
    async fire(id, method, event = {}, ranges) {
      const e = all(ranges).find((x) => typeof x.call === 'function' && x.call('id')?.[0] === id);
      if (!e) throw new Error(`no element with id ${id}`);
      const args = e.call(method);
      if (!args) throw new Error(`${id} has no ${method}`);
      args[args.length - 1]({ modifiers: {}, ...event }, cx);
      await settle();
      render();
    },
    async click(id, ranges) {
      const e = all(ranges).find((x) => typeof x.call === 'function' && x.call('id')?.[0] === id);
      if (!e) throw new Error(`no element with id ${id}`);
      const h = e.call('on_click')?.[0];
      if (!h) throw new Error(`${id} has no on_click`);
      h({}, cx);
      await settle();
      render();
    },
  };
}
