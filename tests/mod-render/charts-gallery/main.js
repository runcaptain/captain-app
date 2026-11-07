// CAPTAIN: every chart the kit draws, for the real-window check (tests/mod-render/forms-check.sh copies this mod and
// the kit into a scratch home). Each chart sits in a magenta frame; charts-shot.py fails any frame whose ink is flat
// or missing. The cyan frame is the self-test: a percent path drawn without size_full(), the bug that drew lines flat,
// which the analyzer must catch.
import { View, div, PathBuilder } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { palette } from "./kit/tokens.js";
import { ChartView } from "./kit/charts.js";
import { Chart, Curve } from "./kit/chart.js";
import { Line } from "./kit/ui.js";

const W = 230, H = 112;
const table = {
  columns: [{ name: "month" }, { name: "searches" }, { name: "orders" }, { name: "size" }],
  rows: [["Jan", 12, 3, 2], ["Feb", 30, 9, 5], ["Mar", 18, 14, 3], ["Apr", 44, 6, 8], ["May", 26, 20, 4], ["Jun", 52, 11, 9]],
};
const numeric = { columns: [{ name: "x" }, { name: "y" }, { name: "z" }], rows: [[1, 10, 2], [2, 40, 6], [3, 22, 3], [4, 55, 9], [5, 31, 4], [6, 8, 7]] };

export default class ChartsGallery extends View {
  render() {
    const c = palette(false);
    const frame = (id, label, chart, colour = "#FF00FF") => v_flex().gap(2)
      .child(div().text_size(10.5).text_color(c.grey500).child(label))
      .child(div().id(`chart-${id}`).w(W + 8).h(H + 8).p(2).border_2().border_color(colour).bg(c.surface).overflow_hidden().child(chart));
    const view = (mark, t = table, extra = {}) => ChartView(c, t, { mark, x: t.columns[0].name, y: [t.columns[1].name, ...(mark === "line" || mark === "bar" || mark === "area" ? [t.columns[2].name] : [])], legend: "none", ...extra }, { width: W, height: H });
    const pts = table.rows.map((r) => ({ x: r[0], y: r[1] }));
    const curve = [0, 0.2, 0.4, 0.6, 0.8, 1].map((x, i) => ({ x, y: [0, 0.03, -0.01, 0.05, 0.02, 0.06][i] }));
    // The bug, on purpose: percentages against a painted element with no size.
    let flat = PathBuilder.stroke(2);
    curve.forEach((p, i) => { const X = `${4 + p.x * 92}%`, Y = `${10 + (1 - (p.y + 0.01) / 0.07) * 80}%`; flat = i ? flat.line_to(X, Y) : flat.move_to(X, Y); });
    const cells = [
      frame("line", "Notebook: line", view("line")),
      frame("area", "Notebook: stacked area", view("area")),
      frame("bar", "Notebook: bar", view("bar")),
      frame("hbar", "Notebook: horizontal bar", view("hbar")),
      frame("pie", "Notebook: pie", view("pie")),
      frame("doughnut", "Notebook: doughnut", view("doughnut")),
      frame("scatter", "Notebook: scatter", view("scatter", numeric)),
      frame("bubble", "Notebook: bubble", view("bubble", numeric, { z: "z" })),
      frame("kit-line", "Kit chart: line", Chart(c, { mark: "line", points: pts, xLabel: "month", yLabel: "searches" }, { height: H - 20 })),
      frame("kit-bar", "Kit chart: bar", Chart(c, { mark: "bar", points: pts, xLabel: "month", yLabel: "searches" }, { height: H - 20 })),
      frame("kit-point", "Kit chart: points", Chart(c, { mark: "point", points: numeric.rows.map((r) => ({ x: r[0], y: r[1] })), xLabel: "x", yLabel: "y" }, { height: H - 20 })),
      frame("ui-line", "Kit Line", Line(c, [12, 30, 18, 44, 26, 52], { height: H - 4 })),
      frame("curve", "Runs: trigger-rate curve", Curve(c, curve, { height: H - 4 })),
      frame("curve-dashed", "Runs: directional curve", Curve(c, curve, { height: H - 4, dashed: true, color: c.fail })),
      frame("selftest-flat", "Self-test: the flat-line bug", div().relative().w_full().h(H - 4)
        .child(div().absolute().inset_0().child(globalThis.window.paint_path(flat.build(), c.blue))), "#00FFFF"),
    ];
    const rows = [];
    for (let i = 0; i < cells.length; i += 4) rows.push(h_flex().gap(16).children(cells.slice(i, i + 4)));
    return v_flex().id("charts-gallery").size_full().bg(c.surface).p(16).gap(12).children(rows);
  }
}
