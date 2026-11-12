// captain-ui: hand a task to an agent, the way people should: a prompt, not a command line.
import { run, notify, knownAgents, openOverlay, shared, agents, hc } from "herdr";
import { withView, viewSnapshot } from "./view.js";

/** A prompt that starts by naming the cst Captain runs, by its path. The agent's shell can have an older cst first on
 *  PATH, without the commands the prompt names. Prompts the services wrote already start this way. */
export function withCst(prompt) {
  const text = String(prompt ?? "");
  const cst = shared("captain.daemon")?.cst;
  if (!cst || !text || text.startsWith("Use this cst:")) return text;
  return `Use this cst: ${cst}\nRun every \`cst\` command below with that exact path (it is also in $CST_BIN). An older \`cst\` may come first on your PATH.\n\n${text}`;
}

/** Open Claude Code or Codex in a new terminal tab of this space, with this prompt as its task. With neither
 *  installed, the prompt goes on the clipboard and a note says where to paste it. */
export async function askAgent(cx, prompt) {
  // What the person has open (workspace, project, screen, selection, the Next line) goes first, so the agent never
  // has to ask "which project?".
  prompt = withCst(withView(prompt));
  const agents = await knownAgents();
  const agent = ["claude", "codex"].map((id) => agents.find((a) => a.installed && (a.id === id || a.name?.toLowerCase().startsWith(id) || a.run?.[0] === id))).find(Boolean);
  if (!agent) {
    cx.write_to_clipboard(prompt);
    notify({ title: "No agent installed here", body: "The prompt is on the clipboard: paste it into Claude Code, Codex, Cursor or Devin.", level: "info" });
    return;
  }
  run("tab.new", { name: agent.name ?? "Agent", argv: agent.run, task: prompt });
  run("layout.show", { id: "work" });
}

/** Put a prompt on the clipboard, and say so. */
export function copyPrompt(cx, prompt) {
  cx.write_to_clipboard(withCst(withView(prompt)));
  notify({ title: "Prompt copied", body: "Paste it into any agent.", level: "info" });
}

/** Open one of Captain's designed forms (captain-forms tasks.js), such as "dataset" or "backtest". */
export function openForm(task, args) {
  openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task, ...(args ? { args } : {}) } });
}

/** Send what the person has open to an agent: the agent tab that's open (the one shown, else the first), or a new
 *  one when there is none. The sidebar's button and the palette's "Ask an agent about this screen". */
export async function askAboutScreen(cx) {
  const snap = viewSnapshot();
  const ask = "Look at what I have open in Captain. Run `cst app context` for the details (and `cst app shot` if a picture helps), then tell me what it shows and what to do next. Change nothing yet.";
  const list = agents();
  const open = list.find((a) => a.focused) ?? list[0];
  if (!open) return askAgent(cx, ask);
  // One line: a terminal reads a newline as Enter, and the agent would get half the message.
  const text = `${ask} ${snap ? snap.replace(/\n- /g, "; ").replace(/\n/g, " ") : ""}`.trim();
  try {
    await hc(["send", open.address, text]);
    run("agent.focus", { terminal: open.address });
    run("layout.show", { id: "work" });
    notify({ title: "Sent to your agent", body: `${open.tab_name || open.title || "The open agent tab"} has this screen.`, level: "info" });
  } catch {
    cx.write_to_clipboard(withCst(withView(ask)));
    notify({ title: "Couldn't reach the agent tab", body: "The message is on the clipboard: paste it into your agent.", level: "info" });
  }
}
