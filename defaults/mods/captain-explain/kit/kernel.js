// captain-ui: a notebook's Jupyter kernel, from a mod. One session per open notebook, over the backend's kernel
// socket (POST /api/kernels, then ws /api/kernels/{session}). Messages, one JSON object each:
//   {type: "status", state: "busy" | "idle"}
//   {type: "stream", cell_id, name: "stdout" | "stderr", text}
//   {type: "table", cell_id, var, columns: [{name, type}], rows: [[...]], row_count, truncated}
//   {type: "display", cell_id, mime, data}
//   {type: "error", cell_id, ename, evalue, traceback: [...]}
//   {type: "done", cell_id, ms}
// Executes are serialized: execute() resolves {ok, ms} on the cell's "done", so Run all can stop at a failure.
import { WebSocket } from "websocket";
import { api, services } from "./cst.js";

export class Kernel {
  /** onMsg(msg, cx) for every message; onState(state) on "off" | "connecting" | "idle" | "busy" | "lost". */
  constructor(notebook, onMsg, onState) {
    Object.assign(this, { notebook, onMsg, onState });
    this.state = "off";
    this.sock = null;
    this.session = null;
    this.opening = null;
    this.waiters = new Map();
  }

  set(state) {
    this.state = state;
    this.onState?.(state);
  }

  /** Opens the session and its socket once, and keeps reading on cx's task. */
  open(cx) {
    if (this.sock) return Promise.resolve(this.sock);
    if (this.opening) return this.opening;
    this.set("connecting");
    this.opening = (async () => {
      const d = await services();
      if (!d.base) throw new Error("Captain's services aren't running.");
      const r = await api("/api/kernels", { body: { notebook: this.notebook } });
      if (!r.ok) throw new Error(r.error || "Couldn't start Python for this notebook.");
      this.session = r.data.session;
      const url = `${d.base.replace(/^http/, "ws")}/api/kernels/${encodeURIComponent(this.session)}?token=${encodeURIComponent(d.token)}`;
      this.sock = await WebSocket.connect(url);
      this.set("idle");
      cx.spawn(async (cx) => this.read(cx));
      return this.sock;
    })().catch((e) => {
      this.set("lost");
      throw e;
    }).finally(() => {
      this.opening = null;
    });
    return this.opening;
  }

  async read(cx) {
    const sock = this.sock;
    while (sock && this.sock === sock) {
      let raw;
      try {
        raw = await sock.read();
      } catch {
        break;
      }
      let m;
      try { m = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw)); } catch { continue; }
      if (m.type === "status") this.set(m.state);
      if (m.type === "error") { const w = this.waiters.get(m.cell_id); if (w) w.failed = true; }
      this.onMsg?.(m, cx);
      if (m.type === "done") {
        const w = this.waiters.get(m.cell_id);
        this.waiters.delete(m.cell_id);
        w?.resolve({ ok: !w.failed, ms: m.ms });
      }
      cx.notify();
    }
    if (this.sock === sock) {
      this.sock = null;
      this.set("lost");
      for (const [, w] of this.waiters) w.resolve({ ok: false, ms: 0, lost: true });
      this.waiters.clear();
      cx.notify();
    }
  }

  /** Runs one cell: {cell_id, kind: "sql" | "python", code, n, conn?, var?}. Resolves {ok, ms} when it's done. */
  async execute(cx, cell) {
    const sock = await this.open(cx);
    return new Promise((resolve) => {
      this.waiters.set(cell.cell_id, { resolve, failed: false });
      sock.write(JSON.stringify({ type: "execute", ...cell })).catch(() => {
        this.waiters.delete(cell.cell_id);
        resolve({ ok: false, ms: 0, lost: true });
      });
    });
  }

  /** Stops the running cell (a KeyboardInterrupt in Python; the query is abandoned). */
  interrupt() {
    this.sock?.write(JSON.stringify({ type: "interrupt" })).catch(() => {});
  }

  /** Ends the session: the socket, then the kernel process. */
  async close() {
    const sock = this.sock;
    this.sock = null;
    try { await sock?.close(); } catch {}
    if (this.session) await api(`/api/kernels/${encodeURIComponent(this.session)}`, { method: "DELETE" });
    this.session = null;
    this.set("off");
  }
}
