import { el } from './el.js';
export const h_flex = () => el('h_flex');
export const v_flex = () => el('v_flex');
/** The list keeps its arguments; the harness calls `render` with a visible range, as layout would. */
export const v_virtual_list = (id, count, size, key, render) => el('v_virtual_list', [id, count, size, key, render]);
export const Scrollbar = { vertical: (id) => el('scrollbar', [id]), horizontal: (id) => el('scrollbar', [id]) };
export const InputState = {
  new: (o = {}) => {
    let v = o.value ?? '';
    const handlers = {};
    return { value: () => v, set_value: (x) => (v = String(x)), on: (ev, f) => ((handlers[ev] = f), true), focus() {}, release: () => true, handlers, placeholder: o.placeholder };
  },
};
export const Input = { new: (s) => el('input', [s]) };
export const VirtualListScrollHandle = { new: () => ({ scrolled: [], scroll_to_item(i, how) { this.scrolled.push([i, how]); }, scroll_to_bottom() {}, release() { return true; } }) };
export const EditorState = {
  new: (o = {}) => {
    let v = o.value ?? '';
    const handlers = {};
    return { value: () => v, set_value: (x) => (v = String(x)), on: (ev, f) => ((handlers[ev] = f), true), release: () => true, handlers, language: o.language };
  },
};
export const Editor = { new: (s) => el('editor', [s]) };
