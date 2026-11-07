// captain-ui: hand a task to an agent, the way people should: a prompt, not a command line.
import { run, notify, knownAgents, openOverlay } from "herdr";

/** Open Claude Code or Codex in a new terminal tab of this space, with this prompt as its task. With neither
 *  installed, the prompt goes on the clipboard and a note says where to paste it. */
export async function askAgent(cx, prompt) {
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
  cx.write_to_clipboard(prompt);
  notify({ title: "Prompt copied", body: "Paste it into any agent.", level: "info" });
}

/** Open one of Captain's designed forms (captain-forms tasks.js), such as "dataset" or "backtest". */
export function openForm(task, args) {
  openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task, ...(args ? { args } : {}) } });
}
