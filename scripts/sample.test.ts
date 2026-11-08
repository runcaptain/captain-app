import { test, expect } from "bun:test";
import { readFile } from "node:fs/promises";
const source = (await readFile(new URL("../defaults/mods/captain-forms/tasks.js", import.meta.url), "utf8"))
  .replace(/^import .*;$/gm, "").replace(/export /g, "");
function tasks(api: Function, current = "my-work") {
  const choices: unknown[] = [];
  const load = new Function("api", "setShared", "shared", "connectionsChanged", `${source}\nreturn TASKS;`);
  return { tasks: load(api, (key: string, value: unknown) => choices.push([key, value]), () => current, () => {}), choices };
}
test("sample task reads command output and selects new workspace", async () => {
  const calls: string[] = [];
  const h = tasks(async (path: string) => {
    calls.push(path);
    return { ok: true, data: path === "/api/commands/run" ? { exit_code: 0, output: { workspace: "sample-north-pole" } } : {} };
  });
  expect((await h.tasks.find((x: any) => x.id === "demo").submit()).ok).toBe(true);
  expect(calls).toEqual(["/api/commands/run", "/api/workspaces/sample-north-pole/use"]);
  expect(h.choices).toEqual([["captain.workspace", "sample-north-pole"]]);
});
test("failed command or workspace selection preserves view", async () => {
  for (const failure of ["command", "switch"]) {
    const h = tasks(async (path: string) => path === "/api/commands/run"
      ? { ok: true, data: failure === "command" ? { exit_code: 1, error: "conflict" } : { exit_code: 0, output: { workspace: "sample-north-pole" } } }
      : { ok: false });
    expect((await h.tasks.find((x: any) => x.id === "demo").submit()).ok).toBe(false);
    expect(h.choices).toEqual([]);
  }
});
test("removing active sample returns to previous workspace", async () => {
  const h = tasks(async () => ({ ok: true, data: { exit_code: 0, output: { current: "my-work" } } }), "sample-north-pole");
  expect((await h.tasks.find((x: any) => x.id === "remove-sample").submit()).ok).toBe(true);
  expect(h.choices).toEqual([["captain.workspace", "my-work"]]);
});
test("removing sample while viewing real work preserves that view", async () => {
  const h = tasks(async () => ({ ok: true, data: { exit_code: 0, output: { current: "my-work" } } }));
  expect((await h.tasks.find((x: any) => x.id === "remove-sample").submit()).ok).toBe(true);
  expect(h.choices).toEqual([]);
});
