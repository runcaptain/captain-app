// CAPTAIN: what every form checks, in one place, so no form can skip it.
//
// - A field is checked as the person types. Its problem is one plain sentence under that field.
// - A name is never refused for its spelling. The words typed are the label, and Captain makes the id from them
//   with the same rule the services use (backend/core/captain_search/naming.py).
// - Nothing the services say in their own words reaches the screen: `sayError` turns it into a sentence, and into
//   the field it belongs under when that can be told.

const MAX = 60;

/** The id Captain makes from what a person typed. Never throws, never empty. Mirrors naming.make_id. */
export function idFrom(label, fallback = "item") {
  const s = String(label ?? "").trim().toLowerCase().replace(/[\s_]+/g, "-").replace(/[^a-z0-9-]/g, "").replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "").slice(0, MAX).replace(/-+$/g, "");
  return s || fallback;
}

export const isId = (s) => /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(String(s ?? "")) && String(s).length <= MAX;

// What an unnamed thing of each kind is called (naming.KINDS).
export const FALLBACK = { cloud: "login", project: "project", compute: "place", input: "input", dataset: "dataset", folder: "folder", model: "model", test: "test", notebook: "notebook", connection: "connection", class: "class", rating: "rating" };

const GENERIC = "That didn't work. Check what you entered and try again. If it keeps happening, tell us.";

/** A failure after submit, as {field, text}: which field it belongs under (or null), in a plain sentence.
 *  `fields` are the form's shown fields. Nothing raw gets through: no "bad ...", no exception, no status code. */
export function sayError(error, fields = []) {
  let t = String(error ?? "").trim();
  const names = fields.filter((f) => f.name);
  let field = null;
  if (/^bad name\b/i.test(t)) return { field: names[0]?.key ?? null, text: "That name can't be used as it is. Change it and try again." };
  if (/^(you already have|there is already|.* already (exists|has))/i.test(t) && names.length) field = names[0].key;
  // "<Label> is required" and the like belong under that field.
  const hit = fields.find((f) => f.label && t.toLowerCase().startsWith(String(f.label).toLowerCase() + " "));
  if (hit) field = hit.key;
  if (/^bad\s/i.test(t) || /Traceback|Exception\b|^\w*Error:|\bat \S+ \(.*:\d+:\d+\)|^[{[]/.test(t)) t = GENERIC;
  // A status code means the sentence around it was the services' own, not one written for a person.
  if (/\((?:error|status|code|HTTP)\s*\d{3}\)|\b(?:HTTP|status|error)\s*(?:code\s*)?[45]\d\d\b|^\d{3}:/i.test(t)) t = GENERIC;
  if (!t) t = GENERIC;
  if (!/[.!?]$/.test(t)) t += ".";
  return { field, text: t.replace(/^./, (x) => x.toUpperCase()) };
}

/** Does this text read as something raw from the services? What the forms' test fails on. */
export const looksRaw = (text) => /(^|\|\s*)bad\s|\bTraceback\b|\bException\b|\w+Error:|\b(?:error|status|HTTP)\s*[45]\d\d\b|\(\s*[45]\d\d\s*\)/.test(String(text ?? ""));
