// CAPTAIN: Project Structure as a dialog (Settings, Project Structure shows the same panel: kit/scopemodel.js).
// Props: {tab?}: types, terms, hierarchy, explorer or fields.
import { View } from "gpui-kit";
import { theme, props, closeOverlay } from "herdr";
import { palette, isDark } from "./kit/tokens.js";
import { ScopeModelPanel } from "./kit/scopemodel.js";

export default class ScopeModel extends View {
  init(_props, cx) {
    this.panel = new ScopeModelPanel(cx, props()?.tab);
  }

  render() {
    return this.panel.render(palette(isDark(theme())), { onClose: () => closeOverlay(props()?.overlay) });
  }
}
