// For the picture check (tests/mod-render/shot-check.sh): the Weights page's `show` prop for a trained model. The
// picture check's services hold no such model, so the page is handed the made-up one's answers by address.
//   node tests/mod-render/fixtures/captain-weights/parts-shot.mjs '{"part": "L0.self_attn"}'
// `edits`: the model can be changed. `edited`: one edit is already in its session, not saved as a version.
import { PARTS, SESSION, partsModel, partsRoutes } from './parts.mjs';

const show = JSON.parse(process.argv[2] ?? '{}');
const opts = { deep: Boolean(show.deep), edits: Boolean(show.edits) };
const base = `/api/weights/models/${PARTS}`, s = `/api/weights/sessions/${SESSION}`;
if (show.edited) partsRoutes(`${s}/edits`, opts, { body: { op: 'set', tensor: 'act.weight', indices: [[1, 2]], value: 0.5, by: 'you' } });
const paths = [base, `${base}/session`, `${base}/trees?ranker=full&trees=3`];
for (const p of partsModel(opts).parts) {
  if (p.kind === 'attention') paths.push(`${s}/attention?layer=${p.layer}&head=${show.head ?? 0}&matrix=query_key&watch=1`);
  else paths.push(`${s}/heatmap?tensor=${p.tensors[0]}&watch=1`);
  if (p.kind === 'embedding') paths.push(`${s}/nearest?tensor=${p.tensors[0]}&row=${Math.min(show.row ?? 0, p.shape[0] - 1)}&top=8&watch=1`);
}
process.stdout.write(JSON.stringify({ show: { model: PARTS, answers: Object.fromEntries(paths.map((p) => [p, partsRoutes(p, opts)])), ...show } }));
