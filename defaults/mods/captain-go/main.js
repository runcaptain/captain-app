// CAPTAIN: opens something by name from the palette ("Open gift-catalog"): records
// what to open in shared "captain.open" {section, ...} for that section's screen,
// switches the sections stack to it, and closes itself at once.
import { View, div } from "gpui-kit";
import { props, run, setShared, closeOverlay } from "herdr";

export default class CaptainGo extends View {
  init(_props, cx) {
    const p = props() ?? {};
    if (p.section) {
      setShared("captain.open", { ...p, at: Date.now() });
      run("layout.select", { stack: "sections", child: p.section });
    }
    cx.timer.after(1, () => closeOverlay(p.overlay));
  }
  render() {
    return div();
  }
}
