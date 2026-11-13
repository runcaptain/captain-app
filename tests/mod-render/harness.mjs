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

let current = null; // the screen the test touched last: see `beat`
export function harness(Mod) {
  const pending = [];
  const timers = [];
  const timings = [];
  const cx = {
    spawn: (f) => pending.push(Promise.resolve().then(() => f(cx))),
    // As the app's: `every` calls again and again at its interval, `after` calls once. Both give back a handle that
    // cancels. (`after` used to be kept and called on every pass like `every`, so a callback that re-armed itself
    // multiplied without end here and nowhere else, and a one-shot wait looked like a poll.)
    timer: {
      every: (ms, f) => { const t = { f, ms: Math.max(1, Number(ms) || 1), every: true, due: performance.now() + (Number(ms) || 0) }; timers.push(t); return handle(t); },
      after: (ms, f) => { const t = { f, ms: Number(ms) || 0, every: false, due: performance.now() + (Number(ms) || 0) }; timers.push(t); return handle(t); },
    },
    notify() {},
    stop_propagation() {},
    focus_handle: () => ({ focus() {} }),
  };
  const handle = (t) => ({ cancel() { t.dead = true; }, drop() { t.dead = true; }, stop() { t.dead = true; } });
  /** Call the timers that are due, as the app does as time passes. `force` calls every one now, due or not: one
   *  pass of the clock for a test that does not want to wait. A one-shot timer is called once, then gone. */
  function fire(force = false) {
    const at = performance.now();
    for (const t of [...timers]) {
      if (t.dead || (!force && at < t.due)) continue;
      if (t.every) t.due = at + t.ms; else t.dead = true;
      t.f(cx);
    }
    for (let i = timers.length - 1; i >= 0; i--) if (timers[i].dead) timers.splice(i, 1);
  }
  // Time passes whether or not the test is looking, as in the app: a timer that comes due is called even while the
  // test awaits something else (a form's task that waits on a job through the live channel, say).
  // Only for the screen the test touched last. In the app each mod runs by itself. Here several are mounted one
  // after another in one process and share the test's fixtures, so an earlier screen's timers must not go on
  // asking for what a later screen's fixtures do not hold. An earlier screen's timers run again when it is touched.
  const me = {};
  current = me;
  const beat = setInterval(() => { if (current !== me) return; try { fire(false); } catch (e) { queueMicrotask(() => { throw e; }); } }, 20);
  beat.unref?.();
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
      current = me;
      mod.init(globalThis.__props ?? {}, cx);
      await settle();
      render();
    },
    /** Draw the named lists for these ranges (as layout would), run the timers, fetch, and render again. */
    async layoutLists(ranges) {
      current = me;
      render();
      all(ranges);
      for (let i = 0; i < 6; i++) {
        fire(true);
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
      current = me;
      // Time passes as it does in the app: a timer that comes due while waiting is called.
      const end = performance.now() + ms;
      for (;;) {
        const left = end - performance.now();
        await new Promise((r) => setTimeout(r, Math.max(0, Math.min(20, left))));
        fire(false);
        if (performance.now() >= end) break;
      }
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
