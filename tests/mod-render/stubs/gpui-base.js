import { el } from './el.js';
export const h_flex = () => el('h_flex');
export const v_flex = () => el('v_flex');
/** The list keeps its arguments; the harness calls `render` with a visible range, as layout would. */
export const v_virtual_list = (id, count, size, key, render) => el('v_virtual_list', [id, count, size, key, render]);
export const Scrollbar = { vertical: (id) => el('scrollbar', [id]), horizontal: (id) => el('scrollbar', [id]) };
export const InputState = {
  new: (o = {}) => {
    if (globalThis.__rendering) throw new Error('InputState.new(...) cannot run during render; create state in init() or in an event handler and keep it on the view');
    let v = o.value ?? '';
    const handlers = {};
    return { value: () => v, set_value: (x) => (v = String(x)), on: (ev, f) => ((handlers[ev] = f), true), focus() {}, release: () => true, handlers, placeholder: o.placeholder };
  },
};
export const Input = { new: (s) => el('input', [s]) };
// Multi-line text (captain-forms' long fields): the same stand-in as InputState, refused while drawing like the app.
export const TextareaState = { new: (...a) => InputState.new(...a) };
export const Textarea = { new: (s) => el('textarea', [s]) };
// An anchored popup (the real one paints above everything): its trigger and its content are children here, so tests
// can read and click both.
export const Popup = {
  new: (id, trigger) => {
    const e = el('popup', [id]).child(trigger);
    e.__self.content = function (x) { if (x) this.kids.push(x); return this.proxy; };
    return e;
  },
};
export const VirtualListScrollHandle = { new: () => ({ scrolled: [], scroll_to_item(i, how) { this.scrolled.push([i, how]); }, scroll_to_bottom() {}, release() { return true; } }) };
export const EditorState = {
  new: (o = {}) => {
    if (globalThis.__rendering) throw new Error('EditorState.new(...) cannot run during render; create state in init() or in an event handler and keep it on the view');
    let v = o.value ?? '';
    const handlers = {};
    return { value: () => v, set_value: (x) => (v = String(x)), on: (ev, f) => ((handlers[ev] = f), true), release: () => true, handlers, language: o.language };
  },
};
export const Editor = { new: (s) => el('editor', [s]) };
