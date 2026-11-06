// Maps the mod runtime's bare imports to the stubs next to this file.
const map = { 'gpui-kit': 'gpui-kit.js', 'gpui-base': 'gpui-base.js', herdr: 'herdr.js', websocket: 'websocket.js' };
export async function resolve(spec, ctx, next) {
  if (map[spec]) return { url: new URL(map[spec], import.meta.url).href, shortCircuit: true };
  return next(spec, ctx);
}
