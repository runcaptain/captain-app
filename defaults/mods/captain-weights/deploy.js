// CAPTAIN: "Deploy this version" on the Weights page. One press saves the session as a new version of the network
// and deploys that version to this computer, as one job (POST /api/projects/{project}/deploy with the session).
// Under the page's top a line says where it stands: deploying, then the address it answers at and a link to its
// card on Deployments, or why it was refused, in the services' own sentence. The page hears the deploy finish on
// the change feed: it does not ask on a clock.
import { div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { run, shared, setShared } from "herdr";
import { plainError } from "./kit/ui.js";
import { MONO } from "./kit/tokens.js";
import { projectOf, listProjects, sendDeploy, readDeploy, watchDeploy } from "./adapter.js";

/** The address a deployed model answers at, from a deploy's status, or null. */
export function addressOf(status) {
  const s = status?.staged ?? {};
  return s.endpoint?.url ?? s.address ?? s.url ?? status?.address ?? null;
}

/** What the line says for a deploy's state: {text, tone: "plain" | "bad", address, open}. */
export function deployWords(dp) {
  if (!dp) return null;
  if (dp.state === "asking") return { text: "Saving this session as a new version and starting its deploy.", tone: "plain" };
  if (dp.state === "pick") return { text: "Deploy into which project?", tone: "plain" };
  if (dp.state === "refused") return { text: dp.text, tone: "bad" };
  if (dp.state === "failed") return { text: dp.text || "The deploy did not finish.", tone: "bad", open: true };
  const what = dp.ref ? `${dp.ref.replace("@v", ", version ")}` : "This version";
  if (dp.state === "running") return { text: `${what} is deploying to this computer.${dp.step ? ` Now: ${dp.step}.` : ""}`, tone: "plain", open: true };
  return { text: dp.address ? `${what} is deployed on this computer, at` : `${what} is deployed on this computer.`, tone: "plain", address: dp.address, open: true };
}

/** Start the deploy. `v` is the page: it keeps `v.deploy` and is told to draw again as that changes. */
export function startDeploy(v, cx, picked = null) {
  if (!v.session || v.deploy?.state === "asking" || v.deploy?.state === "running") return;
  v.deploy = { state: "asking" };
  cx.notify();
  cx.spawn(async (cx) => {
    const found = await projectOf(v.model);
    // A deployment belongs to a project: the one the network is in, else the one picked in the Explorer.
    const scope = shared("captain.scope");
    let project = picked ?? (found.ok ? found.data : null) ?? (scope?.kind === "project" ? scope.id : null);
    if (!project) {
      // A network that is in no project yet, and none picked in the Explorer: ask here, never send the person away.
      const all = await listProjects();
      const rows = all.ok ? all.data : [];
      if (rows.length === 1) project = rows[0].id;
      else if (rows.length > 1) { v.deploy = { state: "pick", projects: rows }; return cx.notify(); }
      else { v.deploy = { state: "refused", text: all.ok ? "A deployment belongs to a project. Make a project first, then deploy again." : plainError(all.error, "Captain couldn't read the projects.") }; return cx.notify(); }
    }
    const r = await sendDeploy(project, v.session.id);
    if (!r.ok) { v.deploy = { state: "refused", text: plainError(r.error, "Captain couldn't start that deploy.") }; return cx.notify(); }
    v.deploy = { state: "running", project, ref: r.data?.saved?.ref ?? null, run: r.data?.run ?? null };
    // The session was saved as a new version: its record changed.
    v.read(cx);
    v.stopDeploy?.();
    v.stopDeploy = watchDeploy(cx, project, (cx) => followDeploy(v, cx));
    followDeploy(v, cx);
    cx.notify();
  });
}

/** Read where the deploy stands, once: at the start and each time the feed says it changed. */
export function followDeploy(v, cx) {
  const dp = v.deploy;
  if (!dp?.project || (dp.state !== "running" && dp.state !== "done")) return;
  cx.spawn(async (cx) => {
    const r = await readDeploy(dp.project);
    if (v.deploy !== dp || !r.ok) return;
    const st = r.data ?? {};
    dp.env = st.env ?? dp.env ?? "staging";
    if (st.status === "done") { dp.state = "done"; dp.address = addressOf(st); v.stopDeploy?.(); v.stopDeploy = null; }
    else if (st.status === "failed") { dp.state = "failed"; dp.text = plainError(st.error, "The deploy did not finish."); v.stopDeploy?.(); v.stopDeploy = null; }
    else if (st.status === "running") dp.step = (st.steps ?? []).find((s) => s.status === "running")?.title ?? null;
    cx.notify();
  });
}

/** The line under the page's top. Null when nothing was deployed from here. */
export function DeployLine(c, v) {
  const w = deployWords(v.deploy);
  if (!w) return null;
  const dp = v.deploy;
  return h_flex().id("wt-deploy").flex_none().px(20).py(7).gap(8).items_center().border_b(1).border_color(c.rule).bg(c.surface2).text_size(12.5)
    .child(div().id("wt-deploy-text").min_w(0).truncate().text_color(w.tone === "bad" ? c.fail : c.navy).tooltip(w.text).child(w.text))
    // An address is something a person copies: the one place the code font belongs.
    .when(Boolean(w.address), (e) => e.child(div().id("wt-deploy-address").flex_none().font_family(MONO).text_size(11.5).child(w.address)))
    .when(dp.state === "pick", (e) => e.children((dp.projects ?? []).slice(0, 8).map((p) => div().id(`wt-deploy-into-${p.id}`).flex_none().px(8).py(2)
      .border(1).border_color(c.rule).text_color(c.navy).cursor_pointer().hover((s) => s.bg(c.blueLight))
      .on_click((_e, cx) => { v.deploy = null; startDeploy(v, cx, p.id); }).child(p.name))))
    .child(div().flex_1())
    .when(Boolean(w.open) && Boolean(dp.project), (e) => e.child(div().id("wt-deploy-open").flex_none().text_color(c.blue).cursor_pointer().hover((s) => s.bg(c.blueLight))
      .on_click(() => { setShared("captain.open", { section: "deployments", open: `${dp.project}:${dp.env ?? "staging"}`, at: Date.now() }); run("layout.select", { stack: "sections", child: "deployments" }); })
      .child("See it in Deployments")))
    .child(div().id("wt-deploy-close").flex_none().text_size(11.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
      .on_click((_e, cx) => { v.stopDeploy?.(); v.stopDeploy = null; v.deploy = null; cx.notify(); }).child("Hide"));
}
