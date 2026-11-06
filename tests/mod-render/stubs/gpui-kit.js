import { el } from './el.js';
export class View {}
export const div = () => el('div');
export const svg = (p) => el('svg', [p]);
export const image = (p) => el('image', [p]);
