// captain-ui: numbers for people, without Intl. The app's JavaScript has no locale data, so toLocaleString("en-US")
// prints 12480 where Node prints 12,480. These group digits themselves.
//   int(12480) "12,480"    num(1234.5678) "1,234.57"    num(0.1, 4) "0.1"    num(2, 2, 2) "2.00"
const group = (digits) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

/** A whole number with thousands separators. Empty for null, undefined and "". */
export function int(n) {
  if (n === null || n === undefined || n === "") return "";
  const x = Number(n);
  if (!Number.isFinite(x)) return String(n);
  return `${x < 0 ? "-" : ""}${group(String(Math.round(Math.abs(x))))}`;
}

/** A number with at most `max` decimals (at least `min`), trailing zeros dropped, thousands grouped. */
export function num(n, max = 2, min = 0) {
  if (n === null || n === undefined || n === "") return "";
  const x = Number(n);
  if (!Number.isFinite(x)) return String(n);
  let [whole, frac = ""] = Math.abs(x).toFixed(max).split(".");
  frac = frac.replace(/0+$/, "");
  while (frac.length < min) frac += "0";
  return `${x < 0 && (Number(whole) || Number(frac)) ? "-" : ""}${group(whole)}${frac ? `.${frac}` : ""}`;
}
