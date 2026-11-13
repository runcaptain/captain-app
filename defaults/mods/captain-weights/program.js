// CAPTAIN: asking an agent to work on the open network, from the page. "Ask an agent" opens a goal box ("What
// should the agent fix?") with the three starters as goals to fill in. "Start the agent" hands the goal to
// Captain's services, which start the agent on this session, hold it to its limits and give the verdict
// themselves. What the agent does lands in the timeline like every other event, the strip says where the run
// stands, and "Stop the agent" stops it. A result Captain keeps stays in the session: nothing is saved as a new
// version from here.
//
// Nothing is read on a clock: the run is followed on the live channel (kit/live.js jobDone), and the session is
// read again when its topic is told.
import { div } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { Button, plainError } from "./kit/ui.js";
import { jobDone } from "./kit/live.js";
import { int } from "./kit/num.js";
import { sendProgram, sendProgramStop, readJob } from "./adapter.js";
import { neuronName } from "./inspect.js";
import { answerOf } from "./net.js";

export const MAX_EDITS = 5;      // the most edits the agent may make in one run
/** The strip's height while it is open. On a narrow page the three goals to fill in take two rows. */
export const stripHeight = (width) => (width < 780 ? 152 : 118);

/** The three starters as goals for the box: [{id, label, goal, needs}]. `needs` is a sentence when the goal
 *  cannot be written yet. Each is written around what is selected, in the words a person would use. */
export function goalStarters(v) {
  const { net, sel, run } = v, e = net.examples[v.inputAt];
  const column = sel && sel.k > 0 ? sel.k : 1;
  const where = sel && sel.k > 0 ? `${neuronName(net, sel.k, sel.j)} (layer ${sel.k}, neuron ${sel.j + 1})` : null;
  const out = run?.a?.at(-1), wrong = out && e ? answerOf(out) !== answerOf(e.y) : false;
  const name = e ? v.inputName(e, v.inputAt) : "";
  return [
    { id: "detects", label: "Find what this neuron detects", needs: where ? null : "Open a neuron first.",
      goal: `Find what ${where} detects. Tell me in one sentence what it responds to and how sure you are. Change nothing.` },
    { id: "fix", label: "Fix this wrong answer with the smallest edit", needs: e ? null : "Pick an input first.",
      goal: wrong ? `It answers wrong on the input "${name}". Fix that with the smallest edit, and leave the other answers as they are.`
        : `Check the answer on the input "${name}". If it is wrong, fix it with the smallest edit, and leave the other answers as they are.` },
    { id: "layer", label: "Explain this layer", needs: null,
      goal: `Explain layer ${column}: what it seems to compute, which neurons do most of the work, and which look dead. Change nothing.` },
  ];
}

/** Whether an agent that Captain started is working in the session: the page's own run, or one the record
 *  shows was started (from a terminal, say) and has no verdict yet. */
export function agentWorking(v) {
  if (v.agentRun && ["starting", "running", "stopping"].includes(v.agentRun.state)) return true;
  if (v.agentRun) return false;
  const last = (v.session?.events ?? []).filter((e) => e.kind === "program").at(-1);
  return Boolean(last) && (last.program != null || /^Captain started the agent/.test(String(last.sentence ?? "")));
}

/** Start the agent with the goal in the box. The input on screen is the one thing it is judged on. */
export function startAgent(v, cx) {
  const goal = String(v.goalInput.value() ?? "").trim();
  if (agentWorking(v) || !v.session) return;
  if (!goal) { v.agentRun = null; v.agentNote = "Say what the agent should fix first."; return cx.notify(); }
  const e = v.net.examples[v.inputAt], session = v.session.id;
  v.agentNote = null;
  v.agentRun = { state: "starting", goal, says: "Starting the agent." };
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await sendProgram(session, { goal, max_edits: MAX_EDITS, ...(e ? { target_input: e.x, target_output: e.y } : {}) });
    const mine = v.agentRun;
    if (!r?.ok || !r.data?.job) { v.agentRun = null; v.agentNote = plainError(r?.error, "Captain couldn't start the agent."); return cx.notify(); }
    Object.assign(mine, { state: "running", job: r.data.job, agent: r.data.agent ?? null, says: "The agent is working." });
    v.read(cx);
    cx.notify();
    // Followed on the live channel: read when the job says something, never on a clock.
    const end = await jobDone(cx, r.data.job, { minutes: 30, read: readJob, onStep: (job) => { if (job?.message && mine.state !== "stopping") mine.says = String(job.message); cx.notify(); } });
    if (v.agentRun !== mine) return;
    if (end.ok) v.agentRun = { state: "done", goal, kept: Boolean(end.result?.kept), inSession: Boolean(end.result?.kept_in_session), stopped: Boolean(end.result?.stopped),
      says: typeof end.result?.reason === "string" && end.result.reason ? end.result.reason : "The agent has finished." };
    else v.agentRun = { state: "failed", goal, says: end.timedOut ? "The agent is still working after 30 minutes. Its steps go on showing below." : plainError(end.message, "The agent stopped before Captain could give a verdict.") };
    v.read(cx);
    cx.notify();
  });
}

/** Stop the agent. Captain's verdict on what it did so far follows, and its edits are taken back. */
export function stopAgent(v, cx) {
  if (!v.session || v.agentRun?.state === "stopping") return;
  const run = v.agentRun ?? (v.agentRun = { state: "running", goal: "", says: "" });
  run.state = "stopping"; run.says = "Stopping the agent.";
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await sendProgramStop(v.session.id);
    if (!r?.ok) { v.agentNote = plainError(r?.error, "Captain couldn't stop the agent."); if (v.agentRun === run) { if (run.job) run.state = "running"; else v.agentRun = { state: "failed", goal: run.goal, says: "" }; } }
    // A run this page did not start has no job to follow: the record says when it ended.
    else if (v.agentRun === run && !run.job) v.agentRun = { state: "done", goal: run.goal, says: "The agent was stopped. Captain's verdict is in the list below." };
    v.read(cx);
    cx.notify();
  });
}

/** The strip under the header: the goal box, the starters as goals to fill in, and where the run stands. */
export function AgentStrip(c, v) {
  const working = agentWorking(v), run = v.agentRun, e = v.net.examples[v.inputAt];
  const fill = (s) => Button(c, `wt-ask-${s.id}`, s.label, (_e, cx) => { v.goalInput.set_value(s.goal); v.agentNote = null; cx.notify(); }, { small: true, disabled: Boolean(s.needs) || working });
  const state = run?.state;
  const says = v.agentNote ?? run?.says ?? (working ? "An agent is working in this session." : "");
  const done = state === "done" ? (run.inSession ? " It is in this session, and is not saved as a version." : "") : "";
  return v_flex().id("wt-agent").h(stripHeight(v.width)).flex_none().px(20).py(8).gap(6).border_b(1).border_color(c.rule).bg(c.surface2)
    .child(h_flex().gap(8).items_center()
      .child(div().id("wt-goal").flex_1().min_w(0).h(28).flex().items_center().px(8).border_1().border_color(c.grey300).bg(c.surface).cursor_text()
        .child(Input.new(v.goalInput).w_full().text_size(12.5).text_color(c.navy)))
      .child(working
        ? Button(c, "wt-agent-stop", "Stop the agent", (_e, cx) => stopAgent(v, cx), { small: true, disabled: state === "stopping" || state === "starting" })
        : Button(c, "wt-agent-start", "Start the agent", (_e, cx) => startAgent(v, cx), { small: true })))
    .child(h_flex().gap(6).items_center().flex_wrap()
      .child(div().flex_none().pr(2).text_size(11.5).text_color(c.grey500).child("Fill in a goal"))
      .children(goalStarters(v).map(fill)))
    .child(says
      ? div().id("wt-agent-says").min_w(0).truncate().text_size(12.5).text_color(v.agentNote || state === "failed" ? c.fail : c.navy).tooltip(`${says}${done}`).child(`${says}${done}`)
      : div().id("wt-agent-about").min_w(0).truncate().text_size(11.5).text_color(c.grey500)
        .child(`Judged on the input on screen${e ? ` (${v.inputName(e, v.inputAt)})` : ""}. At most ${int(MAX_EDITS)} edits. A result that does harm is taken back.`));
}
