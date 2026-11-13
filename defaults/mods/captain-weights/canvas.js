// CAPTAIN: a surface to pan and zoom, for the network drawing. The gestures, the limits, Fit and the overview map
// are the lineage graph's (kit/lineage.js), so the two canvases feel the same: drag the background or scroll to
// pan, hold Command and scroll (or use + and -) to zoom, Fit to see it all. The lineage graph lays out boxes; this
// holds only the view, and the caller draws whatever it likes through `at()`.
import { div } from "gpui-kit";
import { h_flex } from "gpui-base";

const MIN_ZOOM = 0.3, MAX_ZOOM = 2.5;
const MAX_FIT = 1.6; // Fit enlarges a small drawing up to here
const MARGIN = 24; // the space Fit leaves around the drawing
const READABLE = 0.8; // below this zoom the words on the drawing are too small: open at 100% instead
const MAP_W = 120, MAP_H = 76, MAP_MARGIN = 8;

export class PanZoom {
  constructor(id) {
    this.id = id;
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this.press = null; // {x0, y0, ox, oy, moved} while the left button is held on the background
    this.view = null; // the canvas's size
    this.touched = false; // the person has panned or zoomed: the view is theirs until Fit
    this.fitFor = null;
  }

  /** A point of the drawing, on screen. */
  at(x, y) { return [x * this.zoom + this.pan.x, y * this.zoom + this.pan.y]; }

  zoomBy(f, px = 0, py = 0) {
    const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, this.zoom * f));
    this.pan = { x: px - (px - this.pan.x) * (z / this.zoom), y: py - (py - this.pan.y) * (z / this.zoom) };
    this.zoom = z;
  }

  /** Zoom and pan so a drawing `cw` by `ch` sits whole in the middle of a canvas `w` by `h`. */
  fit(cw, ch, w, h) {
    const z = Math.max(MIN_ZOOM, Math.min(MAX_FIT, (w - MARGIN * 2) / Math.max(1, cw), (h - MARGIN * 2) / Math.max(1, ch)));
    this.zoom = z;
    this.pan = { x: (w - cw * z) / 2, y: (h - ch * z) / 2 };
  }

  /** How the view opens, and opens again when the canvas or the drawing changes size, until the person moves it.
   *  A drawing that fits at READABLE or more is fitted whole. One that would have to shrink further opens at 100%,
   *  so its words stay the size of body text, centred on `focus` ({x, y} in the drawing's units, the open neuron).
   *  With no focus it is fitted whole, however small. The rest is a drag away, the overview map says there is more, and Fit shows it
   *  all. Words too small to read are worse than a drawing that needs a drag. */
  settle(cw, ch, w, h, focus = null) {
    this.view = { w, h };
    const key = `${Math.round(w)}x${Math.round(h)}:${Math.round(cw)}x${Math.round(ch)}`;
    if (this.touched || key === this.fitFor) return;
    this.fitFor = key;
    const whole = Math.min((w - MARGIN * 2) / Math.max(1, cw), (h - MARGIN * 2) / Math.max(1, ch));
    // With nothing open there is nothing to centre on: the whole drawing, small, is a better start than a
    // window onto its middle.
    if (whole >= READABLE || !focus) return this.fit(cw, ch, w, h);
    this.zoom = 1;
    const fx = focus?.x ?? cw / 2, fy = focus?.y ?? ch / 2;
    // Centred on the focus, but never past the drawing's own edges.
    const within = (want, size, room) => (size <= room - MARGIN * 2 ? (room - size) / 2 : Math.min(MARGIN, Math.max(room - MARGIN - size, want)));
    this.pan = { x: within(w / 2 - fx, cw, w), y: within(h / 2 - fy, ch, h) };
  }

  /** How a drawing of blocks opens: fitted whole when its words stay readable at that size (`floor` or more).
   *  Otherwise at `floor`, the smallest size its words can be read at, from its left edge (or centred on `at`,
   *  a point of the drawing), with its top in view. Like `settle`, it opens again only when the canvas or the
   *  drawing changes size, and never once the person has moved the view. */
  settleReadable(cw, ch, w, h, floor, at = null) {
    this.view = { w, h };
    const key = `${Math.round(w)}x${Math.round(h)}:${Math.round(cw)}x${Math.round(ch)}:${at ? `${Math.round(at.x)},${Math.round(at.y)}` : ""}`;
    if (this.touched || key === this.fitFor) return;
    this.fitFor = key;
    const whole = Math.min((w - MARGIN * 2) / Math.max(1, cw), (h - MARGIN * 2) / Math.max(1, ch));
    if (whole >= floor) return this.fit(cw, ch, w, h);
    this.zoom = floor;
    // Along each way: centred when it fits, else from its start (or around `at`), never past its own edges.
    const along = (size, room, want) => (size * floor <= room - MARGIN * 2 ? (room - size * floor) / 2 : Math.min(MARGIN, Math.max(room - MARGIN - size * floor, want)));
    this.pan = { x: along(cw, w, at ? w / 2 - at.x * floor : MARGIN), y: along(ch, h, at ? h / 2 - at.y * floor : MARGIN) };
  }

  /** Give a canvas element the gestures. */
  gestures(e) {
    return e
      .when(this.press?.moved, (x) => x.cursor_grabbing())
      .on_mouse_down("left", (ev) => { this.press = { x0: ev.position?.x ?? 0, y0: ev.position?.y ?? 0, ox: this.pan.x, oy: this.pan.y, moved: false }; })
      .on_mouse_move((ev, cx) => {
        const p = this.press;
        if (!p) return;
        const dx = (ev.position?.x ?? 0) - p.x0, dy = (ev.position?.y ?? 0) - p.y0;
        if (!p.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
        p.moved = true;
        this.touched = true;
        this.pan = { x: p.ox + dx, y: p.oy + dy };
        cx.notify();
      })
      .on_mouse_up("left", (_ev, cx) => { this.mapDrag = false; const p = this.press; this.press = null; if (p?.moved) cx.notify(); })
      .on_hover((over, cx) => { if (!over && this.press) { this.press = null; cx.notify(); } })
      .on_scroll_wheel((ev, cx) => {
        cx.stop_propagation?.();
        const d = ev.delta ?? { x: 0, y: 0 };
        this.touched = true;
        if (ev.modifiers?.platform || ev.modifiers?.control) {
          const lp = ev.local_position ?? (ev.bounds && ev.position ? { x: ev.position.x - ev.bounds.x, y: ev.position.y - ev.bounds.y } : { x: 0, y: 0 });
          this.zoomBy(Math.exp(d.y * 0.005), lp.x, lp.y);
        } else this.pan = { x: this.pan.x + d.x, y: this.pan.y + d.y };
        cx.notify();
      });
  }

  /** The zoom buttons, for the canvas's top right corner. */
  buttons(c, cw, ch) {
    const w = this.view?.w ?? cw, h = this.view?.h ?? ch;
    const btn = (k, text, tip, go) => div().id(`${this.id}-zoom-${k}`).h(24).min_w(24).px(6).flex().items_center().justify_center().cursor_pointer()
      .bg(c.surface).border_1().border_color(c.grey300).text_size(12).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue))
      .tooltip(tip).on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
      .on_click((_e, cx) => { go(); cx.notify(); }).child(text);
    return h_flex().absolute().top(8).right(8).gap(4)
      .child(btn("out", "-", "Zoom out", () => { this.touched = true; this.zoomBy(1 / 1.2, w / 2, h / 2); }))
      .child(btn("in", "+", "Zoom in", () => { this.touched = true; this.zoomBy(1.2, w / 2, h / 2); }))
      .child(btn("fit", "Fit", "Show the whole network", () => { this.touched = true; this.fit(cw, ch, w, h); }));  // the view is now the person's: the next draw must not open it again
  }

  /** A small map of the whole drawing with the part on screen outlined, when it does not all fit. `marks` are
   *  [{x, y, w, h, on}] in the drawing's own units. A press there moves the view to it. */
  minimap(c, cw, ch, marks) {
    const w = this.view?.w, h = this.view?.h, z = this.zoom;
    if (!w || (cw * z <= w && ch * z <= h && this.pan.x >= 0 && this.pan.y >= 0 && this.pan.x + cw * z <= w && this.pan.y + ch * z <= h)) return null;
    const IN = 6, s = Math.min((MAP_W - IN * 2) / cw, (MAP_H - IN * 2) / ch);
    const ox = (MAP_W - cw * s) / 2, oy = (MAP_H - ch * s) / 2;
    const go = (ev, cx) => {
      const lp = ev.local_position ?? (ev.position && ev.bounds ? { x: ev.position.x - ev.bounds.x, y: ev.position.y - ev.bounds.y } : null);
      if (!lp) return;
      this.touched = true;
      this.pan = { x: -((lp.x - ox) / s) * z + w / 2, y: -((lp.y - oy) / s) * z + h / 2 };
      cx.notify();
    };
    return div().id(`${this.id}-minimap`).absolute().right(MAP_MARGIN).bottom(MAP_MARGIN).w(MAP_W).h(MAP_H).bg(c.surface).border_1().border_color(c.grey500).overflow_hidden()
      .on_mouse_down("left", (ev, cx) => { cx.stop_propagation?.(); this.mapDrag = true; go(ev, cx); })
      .on_mouse_move((ev, cx) => { if (this.mapDrag) { cx.stop_propagation?.(); go(ev, cx); } })
      .on_mouse_up("left", () => { this.mapDrag = false; })
      .children(marks.map((m) => div().absolute().left(ox + m.x * s).top(oy + m.y * s).w(Math.max(2, m.w * s)).h(Math.max(2, m.h * s)).bg(m.on ? c.blue : c.grey500)))
      .child(div().absolute().left(ox - this.pan.x / z * s).top(oy - this.pan.y / z * s).w(w / z * s).h(h / z * s).border_1().border_color(c.blue));
  }
}
