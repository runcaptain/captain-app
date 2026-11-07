// CAPTAIN: team views (plan 0, spec S8): "an engineer or agent builds a project-specific view through code, without
// changing the desktop core." A project's captain/ folder (GET /api/extensions -> project.path) may hold
// views/<name>/, each an ordinary mod (mod.json and main.js) whose mod.json says "captain": {"view_api": 1}.
//
// This copies each one into the app's mods folder as team-<name>, with the Captain kit as ./kit (views import it the
// way Captain's own screens do), and copies again whenever a file changes, so the app's own hot reload picks it up.
// A changed view is checked first (`captain-app check`, with the kit): one that fails isn't copied, so its last good
// version stays installed, and the check's words go with it to the sidebar. The others never notice. Views that leave the project are removed (only folders this wrote, marked
// .captain-team-view). The list is published as shared "captain.team.views" for the sidebar.
import { exec, appHome, appBinary, setShared, shared } from "herdr";
import { api } from "./kit/cst.js";

const MARK = ".captain-team-view";
const API = 1;
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");

// One pass in a shell: for each view, a signature of its files (so unchanged views aren't copied), and the copy.
const SIG = `cd "$1" && find . -type f ! -path './kit/*' ! -path './.git/*' ! -path './node_modules/*' -print0 | sort -z | xargs -0 stat -f '%m %z %N' 2>/dev/null | shasum | cut -c1-16`;
const STAGE = `set -e; src="$1"; dst="$2"; kit="$3"; mkdir -p "$dst"
rsync -a --delete --exclude kit --exclude .git --exclude node_modules --exclude ${MARK} "$src/" "$dst/"
rm -rf "$dst/kit"; cp -R "$kit" "$dst/kit"; printf '%s\\n' "$src" > "$dst/${MARK}"`;

export class TeamViews {
  constructor() { this.sigs = {}; this.failed = {}; this.project = undefined; this.busy = false; }

  start(cx) {
    this.sync(cx);
    cx.timer.every(2000, (cx) => this.sync(cx));
  }

  sync(cx) {
    if (this.busy) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      try {
        await this.pass();
      } catch (e) {
        setShared("captain.team.error", String(e?.message ?? e));
      } finally {
        this.busy = false;
        cx.notify();
      }
    });
  }

  async pass() {
    // The project folder: asked again every 30 s (a `cst project use` changes it).
    if (this.project === undefined || Date.now() - (this.askedAt ?? 0) > 30000) {
      const r = await api("/api/extensions", { scoped: false });
      if (r.ok) { this.project = r.data?.project?.path ?? null; this.askedAt = Date.now(); }
      else if (this.project === undefined) return;
    }
    const mods = `${appHome()}/mods`;
    const views = [];
    if (this.project) {
      const found = await exec("find", [`${this.project}/views`, "-mindepth", "2", "-maxdepth", "2", "-name", "mod.json"]).catch(() => null);
      for (const line of (found?.code === 0 ? found.stdout : "").split("\n").map((l) => l.trim()).filter(Boolean)) {
        const dir = line.replace(/\/mod\.json$/, "");
        const name = slug(dir.split("/").pop());
        if (!name) continue;
        let manifest = {};
        try { manifest = JSON.parse((await exec("cat", [line])).stdout); } catch { manifest = { broken: true }; }
        views.push({ name, dir, mod: `team-${name}`, title: manifest.title ?? name, icon: manifest.icon ?? "layers",
          size: manifest.overlay ?? { width: 0.8, height: 0.85 }, api: manifest.captain?.view_api ?? null,
          problem: manifest.broken ? "Its mod.json isn't valid JSON." : manifest.captain?.view_api == null ? `Its mod.json needs "captain": {"view_api": ${API}}.`
            : manifest.captain.view_api > API ? `It needs view API ${manifest.captain.view_api}; this Captain understands ${API}. Update Captain.` : null });
      }
    }
    // Copy what changed (and nothing for a view this Captain can't run).
    for (const v of views) {
      if (v.problem) continue;
      const sig = (await exec("sh", ["-c", SIG, "sig", v.dir]).catch(() => null))?.stdout?.trim();
      if (!sig || sig === this.sigs[v.mod]) continue;
      if (this.failed[v.mod]?.sig === sig) { v.problem = this.failed[v.mod].why; continue; }
      // Check it as the app will load it; a failure keeps the last good version installed.
      const checked = await exec(appBinary(), ["check", v.dir], { timeout: 60000 }).catch((e) => ({ code: 1, stderr: String(e?.message ?? e) }));
      if (checked.code !== 0) {
        const why = `${checked.stdout ?? ""}\n${checked.stderr ?? ""}`.split("\n").map((l) => l.trim())
          .filter((l) => l && !/^team view .* checked with the Captain kit:$/.test(l) && !/^check failed/.test(l)).slice(0, 3).join(" ");
        this.failed[v.mod] = { sig, why: why || "It didn't pass `captain-app check`." };
        v.problem = this.failed[v.mod].why;
        continue;
      }
      delete this.failed[v.mod];
      const r = await exec("sh", ["-c", STAGE, "stage", v.dir, `${mods}/${v.mod}`, `${mods}/captain-nav/kit`]).catch((e) => ({ code: 1, stderr: String(e) }));
      if (r.code === 0) this.sigs[v.mod] = sig;
      else v.problem = `Captain couldn't load it: ${(r.stderr || "").trim().split("\n").pop()}`;
    }
    // Remove views that left the project: only folders this wrote.
    const keep = new Set(views.map((v) => v.mod));
    const old = await exec("sh", ["-c", `for d in "$1"/team-*; do [ -f "$d/${MARK}" ] && basename "$d"; done; true`, "old", mods]).catch(() => null);
    for (const m of (old?.stdout ?? "").split("\n").map((s) => s.trim()).filter(Boolean)) {
      if (!keep.has(m)) { await exec("rm", ["-rf", `${mods}/${m}`]).catch(() => {}); delete this.sigs[m]; }
    }
    // Whether a version of each is installed (the last good one, when it has a problem now).
    const have = new Set((await exec("ls", [mods]).catch(() => ({ stdout: "" }))).stdout.split("\n").map((x) => x.trim()));
    const list = views.map(({ name, mod, title, icon, size, dir, problem }) => ({ name, mod, title, icon, size, source: dir, problem, installed: have.has(mod) }));
    if (JSON.stringify(list) !== JSON.stringify(shared("captain.team.views") ?? [])) setShared("captain.team.views", list);
    setShared("captain.team.project", this.project);
    setShared("captain.team.error", null);
  }
}
