import { el } from './el.js';
export class View {}
export const div = () => el('div');
export const svg = (p) => el('svg', [p]);
export const image = (p) => el('image', [p]);

/** Path geometry, recorded: each call is kept so a test can read the shape. */
function builder(kind, width) {
  const b = { kind, width, ops: [] };
  for (const m of ['move_to', 'line_to', 'curve_to', 'cubic_bezier_to', 'arc_to', 'add_polygon', 'close', 'dash_array']) b[m] = (...a) => (b.ops.push([m, a]), b);
  b.build = () => ({ kind, width, ops: b.ops.slice() });
  return b;
}
export const PathBuilder = { fill: () => builder('fill'), stroke: (w) => builder('stroke', w) };
// The window's paint_path, as a recorded element.
globalThis.window = { ...(globalThis.window ?? {}), paint_path: (path, color) => el('path', [path, color]) };
