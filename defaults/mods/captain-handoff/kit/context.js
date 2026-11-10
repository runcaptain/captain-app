// captain-ui: the agent's context, from the app's side. A person pins things for their agent ("Send to agent" on a
// dataset, an input, a candidate, a comparison, a run, an experiment); the sidebar's Agent context strip lists them
// with the current screen. `cst context show` reads the same list and expands each to a summary.
//
// The list is one small file per workspace in the app's own folder, <app home>/context/<workspace>.json, written by
// this file and by `cst context add|rm|clear` (backend/core/captain_search/agent_context.py):
//   {"items": [{ref: "dataset:demo-catalog@v1", kind: "dataset", label: "demo-catalog@v1", added_by, added_at}]}
// A ref is "<kind>:<name>": dataset, model, run, experiment, candidate, table, project, file. Never a secret.
import { appHome, readTextFile, writeTextFile, exec, shared, setShared, run, notify, agents } from "herdr";
import { div, svg } from "gpui-kit";
import { askAgent } from "./agent.js";
import { icon } from "./icons.js";

const MAX = 40;
const folder = () => `${appHome()}/context`;
const file = () => `${folder()}/${String(shared("captain.workspace") ?? "default").replace(/[^A-Za-z0-9._-]/g, "_")}.json`;

/** The pinned things for this window's workspace, oldest first. Never throws: no file is no pins. */
export async function loadPins() {
  try {
    const text = await readTextFile(file());
    const items = text ? JSON.parse(text)?.items : [];
    return Array.isArray(items) ? items.filter((i) => i && typeof i.ref === "string") : [];
  } catch { return []; }
}

async function savePins(items) {
  try { await exec("mkdir", ["-p", folder()]); } catch { /* it exists, or the write below says why not */ }
  await writeTextFile(file(), JSON.stringify({ items: items.slice(-MAX) }, null, 2));
  // Every open screen's strip reloads now, not at its next look.
  setShared("captain.context.changed", Date.now());
}

/** Pin {ref, kind, label} for the agent. Pinning it again moves it to the end. */
export async function pin(item) {
  const items = (await loadPins()).filter((i) => i.ref !== item.ref);
  items.push({ ref: item.ref, kind: item.kind, label: item.label ?? item.ref, added_by: "you, in Captain", agent: null, added_at: new Date().toISOString() });
  await savePins(items);
  return items;
}

export async function unpin(ref) {
  const items = (await loadPins()).filter((i) => i.ref !== ref);
  await savePins(items);
  return items;
}

export async function clearPins() {
  await savePins([]);
  return [];
}

/** One line for the agent's input: what was pinned and how to read it. No newline: the person adds their ask. */
export const referenceLine = (item) => `[Captain: ${item.kind} ${item.label ?? item.ref} is pinned for you. Run \`cst context show\` to read it.] `;

/** "Send to agent": pin the item and put a one-line reference in the open agent tab's input, for the person to
 *  finish and send. With no agent tab open, one opens with the reference as its first message. */
export async function sendToAgent(cx, item) {
  try { await pin(item); } catch { notify({ title: "Couldn't pin that", body: "Captain couldn't write its context file.", level: "error" }); return; }
  const list = agents();
  const open = list.find((a) => a.focused) ?? list[0];
  if (!open) {
    await askAgent(cx, `The person pinned ${item.kind} ${item.label ?? item.ref} for you in Captain. Run \`cst context show\` to read it, then ask what they want done with it. Change nothing yet.`);
    return;
  }
  // The terminal takes it as a paste: nothing is sent until the person presses Enter.
  cx.write_to_clipboard(referenceLine(item));
  run("layout.show", { id: "work" });
  run("agent.focus", { terminal: open.address });
  run("terminal.paste", {});
  notify({ title: "Sent to your agent", body: `${item.label ?? item.ref} is pinned, and a reference is in the agent's input. Add what you want done and press Enter.`, level: "info" });
}

/** The small "Send to agent" action for a thing a person is looking at. `item`: {ref, kind, label}. A quiet link
 *  with the agent mark, so it sits beside a screen's own buttons without competing with them. */
export function SendToAgent(c, id, item) {
  return div().id(id).h(26).px(8).flex().flex_none().items_center().gap(5).text_size(12).font_medium().text_color(c.blue).cursor_pointer()
    .hover((s) => s.bg(c.blueLight)).tooltip("Pin this for your agent and put a reference in its input")
    .on_click((_e, cx) => { cx.stop_propagation?.(); cx.spawn(async (cx) => { await sendToAgent(cx, item); }); })
    .child(svg(icon("bot")).size(13).flex_none())
    .child("Send to agent");
}
