// For the picture check (tests/mod-render/shot-check.sh): the Weights page's `show` prop for a model of parts.
// The picture check's services hold no such model, so the page is handed the made-up one's answers by address.
//   node tests/mod-render/fixtures/captain-weights/parts-shot.mjs '{"part": "attn-1"}'
import { PARTS, partsModel, partsRoutes } from './parts.mjs';

const show = JSON.parse(process.argv[2] ?? '{}');
const base = `/api/weights/models/${PARTS}`;
const answers = { [base]: partsRoutes(base) };
for (const p of partsModel().parts) {
  const t = p.tensors[0], head = t.per_head ? `&head=${show.head ?? 0}` : '';
  const paths = [`${base}/parts/${p.id}/heatmap?tensor=${t.id}${head}&max=64`, `${base}/parts/${p.id}/rows?tensor=${t.id}${head}&top=8`];
  if (p.kind === 'embedding') paths.push(`${base}/parts/${p.id}/nearest?row=${Math.min(show.row ?? 0, p.shape[0] - 1)}&top=8`);
  for (const path of paths) answers[path] = partsRoutes(path);
}
process.stdout.write(JSON.stringify({ show: { model: PARTS, answers, ...show } }));
