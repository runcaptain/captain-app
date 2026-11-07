// A recording stand-in for gpui-kit elements: every builder call is kept, children and text are collected, and
// `when` runs its callback like the real one. Enough to render a mod in Node and read what it would show.
export class El {
  constructor(kind, args = []) {
    this.kind = kind;
    this.args = args;
    this.calls = [];
    this.kids = [];
    return new Proxy(this, {
      get(t, k) {
        if (k in t) return t[k];
        if (typeof k === 'symbol') return undefined;
        // The app refuses click handlers registered inside a virtual list's item renderer: use on_item_click.
        if (k === 'on_click' && globalThis.__inItemRenderer) throw new Error("`on_click` cannot be registered from a virtual list's item renderer");
        return (...a) => {
          t.calls.push([k, a]);
          return t.proxy;
        };
      },
    });
  }
}
export function el(kind, args) {
  const e = new El(kind, args);
  e.__self.proxy = e;
  return e;
}
El.prototype.child = function (x) {
  if (x !== null && x !== undefined && x !== false) this.kids.push(x);
  return this.proxy;
};
El.prototype.children = function (xs) {
  for (const x of xs ?? []) this.child(x);
  return this.proxy;
};
El.prototype.when = function (cond, f) {
  return cond ? f(this.proxy) : this.proxy;
};
Object.defineProperty(El.prototype, '__self', { get() { return this; } });
/** Calls by name, e.g. call(e, "id"). */
El.prototype.call = function (name) {
  return this.calls.find(([k]) => k === name)?.[1];
};
