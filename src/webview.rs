//! CAPTAIN: webview blocks. `{"webview": {"url": "...", "id": "..."}}` in a
//! layout puts a native web view (WKWebView, through wry) over the block's
//! laid-out bounds. The view is a child of GPUI's NSView, so it draws above the
//! Metal surface: GPUI can't paint over it.
//!
//! Each frame, the block's canvas reports its bounds while laying out
//! (`saw`), and one pass at the end of the frame (`sync`, the root's last
//! child) places every view:
//! - seen this frame and not covered: shown at its bounds;
//! - not seen (a stack's other child, a hidden block, another tab): hidden,
//!   kept alive with its page state, like a mod;
//! - covered by a dialog, popup, overlay window or floating block: hidden
//!   while it's covered, because those draw under any native view. Toasts don't
//!   hide it (a toast over a web view isn't seen);
//! - gone from the layout: dropped.
//!
//! Window moves need nothing: a child view moves with its window. Zoom: the
//! page follows the app's zoom.
//!
//! Keys: while the page has the keyboard, GPUI gets no key events. A script in
//! the page catches the escape hatch and every `global` / `layout:<name>` key
//! (not the page's own editing keys) and posts it back; the window replays it
//! with `dispatch_keystroke`, so it runs exactly as if the window had it, then
//! takes the keyboard back. No system-wide shortcut is involved.
//!
//! URLs: http(s) on localhost, 127.0.0.1 or [::1], plus the hosts in the
//! person's `webview_hosts`. Navigation elsewhere and new windows are refused.

use crate::layout::WebviewSpec;
use gpui::{Bounds, Pixels};
use std::collections::{BTreeSet, HashMap, HashSet};

/// Hosts a web view may load without any setting.
const LOCAL: &[&str] = &["localhost", "127.0.0.1", "[::1]"];

/// Keys the page itself needs. Never taken from it, even when bound globally.
const PAGE_KEYS: &[&str] = &["cmd-a", "cmd-c", "cmd-v", "cmd-x", "cmd-z", "shift-cmd-z"];

/// Whether a web view may load `url`. Err: why not, for the block to show.
pub fn allowed(url: &str, extra_hosts: &[String]) -> Result<(), String> {
    if url == "about:blank" {
        return Ok(());
    }
    let parsed = url::Url::parse(url).map_err(|e| format!("{url} is not a URL: {e}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!("{url}: web views load http and https only"));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(format!("{url}: a web view URL can't carry a user name or password"));
    }
    let host = match parsed.host() {
        Some(url::Host::Ipv6(a)) => format!("[{a}]"),
        Some(h) => h.to_string().to_ascii_lowercase(),
        None => return Err(format!("{url} has no host")),
    };
    let ok = LOCAL.contains(&host.as_str()) || extra_hosts.iter().any(|h| h.trim().eq_ignore_ascii_case(&host));
    if ok {
        Ok(())
    } else {
        Err(format!("{host} is not allowed: web views load localhost only, unless settings.json lists the host in webview_hosts"))
    }
}

/// A key as keys.json writes it, in one fixed form: `ctrl-alt-shift-cmd-<key>`.
/// None for chords (several keystrokes) and empty keys.
pub fn canon(keys: &str) -> Option<String> {
    let s = keys.trim().to_ascii_lowercase();
    if s.is_empty() || s.contains(' ') {
        return None;
    }
    const NAMES: [(&[&str], usize); 4] = [
        (&["ctrl-", "control-"], 0),
        (&["alt-", "option-", "opt-"], 1),
        (&["shift-"], 2),
        (&["cmd-", "super-", "win-", "secondary-", "meta-"], 3),
    ];
    let mut rest = s.as_str();
    let mut held = [false; 4];
    'strip: loop {
        for (names, i) in NAMES {
            for n in names {
                // "cmd--" is cmd with the minus key: keep a key after the last dash.
                if rest.len() > n.len() && rest.starts_with(n) {
                    rest = &rest[n.len()..];
                    held[i] = true;
                    continue 'strip;
                }
            }
        }
        break;
    }
    let mut out = String::new();
    for (i, name) in ["ctrl-", "alt-", "shift-", "cmd-"].iter().enumerate() {
        if held[i] {
            out.push_str(name);
        }
    }
    out.push_str(rest);
    Some(out)
}

/// The keys a page hands back to the window: the escape hatch, then `global`
/// and `layout:<layout>` bindings, without the page's editing keys or keys
/// the program would get anyway (`"terminal"`).
pub fn forwarded_keys(listing: &[(String, String, String)], layout: &str) -> Vec<String> {
    let mut out = BTreeSet::new();
    out.insert(canon(crate::keys::ESCAPE_HATCH).expect("the hatch parses"));
    let layout_section = format!("layout:{layout}");
    for (section, keys, action) in listing {
        if section != "global" && *section != layout_section {
            continue;
        }
        if action == "terminal" {
            continue;
        }
        if let Some(k) = canon(keys).filter(|k| !PAGE_KEYS.contains(&k.as_str())) {
            out.insert(k);
        }
    }
    out.into_iter().collect()
}

/// The sections a page may switch to (the `sections` stack in layouts/captain.json).
pub const SECTIONS: &[&str] = &["investigations", "data", "runs", "comparisons", "handoff"];

/// A message the page posted to `messageHandlers.captain`, as a command to run.
/// Only `captain:navigate` to a known section does anything.
pub fn page_message(msg: &serde_json::Value) -> Option<(String, serde_json::Value)> {
    if msg.get("type")?.as_str()? != "captain:navigate" {
        return None;
    }
    let screen = msg.get("screen")?.as_str()?;
    SECTIONS.contains(&screen).then(|| ("layout.select".to_owned(), serde_json::json!({ "stack": "sections", "child": screen })))
}

/// Is this view under something GPUI draws (which it would hide)?
pub fn covered(view: &Bounds<Pixels>, covers: &[Bounds<Pixels>]) -> bool {
    covers.iter().any(|c| c.intersects(view) && c.size.width.as_f32() > 0.0 && c.size.height.as_f32() > 0.0)
}

/// Runs in every page before its own scripts: hands bound keys back.
fn page_script(keys: &[String]) -> String {
    let list = serde_json::to_string(keys).unwrap_or_else(|_| "[]".into());
    format!(
        r#"(() => {{
  const keys = new Set({list});
  const named = {{ Escape: "escape", Enter: "enter", Tab: "tab", Backspace: "backspace", Delete: "delete",
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", PageUp: "pageup",
    PageDown: "pagedown", Home: "home", End: "end", " ": "space" }};
  const punct = {{ Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\", Semicolon: ";",
    Quote: "'", Comma: ",", Period: ".", Slash: "/", Backquote: "`", Space: "space" }};
  const keyOf = (e) => named[e.key] || (/^F\d+$/.test(e.key) ? e.key.toLowerCase()
    : e.code.startsWith("Key") ? e.code.slice(3).toLowerCase()
    : e.code.startsWith("Digit") ? e.code.slice(5) : punct[e.code]);
  addEventListener("keydown", (e) => {{
    if (e.isComposing) return;
    const k = keyOf(e);
    if (!k) return;
    const s = (e.ctrlKey ? "ctrl-" : "") + (e.altKey ? "alt-" : "") + (e.shiftKey ? "shift-" : "") + (e.metaKey ? "cmd-" : "") + k;
    if (!keys.has(s)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    window.ipc.postMessage(JSON.stringify({{ key: s }}));
  }}, true);
  window.__captainKeys = (list) => {{ keys.clear(); for (const k of list) keys.add(k); }};
  // The React app posts {{type: "captain:navigate", ...}} to messageHandlers.captain.
  // wry only gives pages window.ipc, so relay that handler into it.
  const relay = {{ postMessage: (m) => window.ipc.postMessage(JSON.stringify({{ captain: m }})) }};
  try {{
    const h = window.webkit && window.webkit.messageHandlers;
    if (h && !h.captain) Object.defineProperty(h, "captain", {{ value: relay }});
    else if (!window.webkit) window.webkit = {{ messageHandlers: {{ captain: relay }} }};
  }} catch (_) {{}}
}})();"#
    )
}

/// What a block reported while laying out this frame.
struct Seen {
    spec: WebviewSpec,
    bounds: Bounds<Pixels>,
}

/// Every web view of one window, by id.
#[derive(Default)]
pub struct Webviews {
    frame: HashMap<String, Seen>,
    /// Why a view couldn't be made (a refused URL), for its block to show.
    errors: HashMap<String, String>,
    /// Shared with each page's message handler: what it may hand back.
    keys: std::rc::Rc<std::cell::RefCell<HashSet<String>>>,
    sent_keys: Vec<String>,
    #[cfg(target_os = "macos")]
    views: HashMap<String, imp::View>,
}

/// The id a webview block's view goes by.
pub fn view_id(block: &str, spec: &WebviewSpec) -> String {
    spec.id.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| block.to_owned())
}

impl Webviews {
    /// A webview block laid out at `bounds` this frame.
    pub fn saw(&mut self, id: String, spec: WebviewSpec, bounds: Bounds<Pixels>) {
        self.frame.insert(id, Seen { spec, bounds });
    }

    pub fn error(&self, id: &str) -> Option<&str> {
        self.errors.get(id).map(String::as_str)
    }

    /// Every view's id, for commands and errors.
    pub fn ids(&self) -> Vec<String> {
        #[cfg(target_os = "macos")]
        {
            let mut ids: Vec<String> = self.views.keys().cloned().collect();
            ids.sort();
            ids
        }
        #[cfg(not(target_os = "macos"))]
        Vec::new()
    }

    /// The view a command means: `id`, or the only one there is.
    pub fn pick(&self, id: Option<&str>) -> Result<String, String> {
        let ids = self.ids();
        match id {
            Some(id) if ids.iter().any(|i| i == id) => Ok(id.to_owned()),
            Some(id) => Err(format!("no web view {id:?}; there are: {}", if ids.is_empty() { "none".into() } else { ids.join(", ") })),
            None if ids.len() == 1 => Ok(ids[0].clone()),
            None if ids.is_empty() => Err("no web view is open".into()),
            None => Err(format!("several web views are open; give an id: {}", ids.join(", "))),
        }
    }

    /// End of a frame: place, show and hide every view (see the module doc).
    /// `live`: ids of every webview block in the layout, shown or not.
    #[allow(clippy::too_many_arguments)]
    pub fn sync(&mut self, window: &gpui::Window, covers: &[Bounds<Pixels>], live: &HashSet<String>, keys: Vec<String>, zoom: f32, hosts: &[String]) {
        let frame = std::mem::take(&mut self.frame);
        if keys != self.sent_keys {
            *self.keys.borrow_mut() = keys.iter().cloned().collect();
        }
        #[cfg(target_os = "macos")]
        {
            self.views.retain(|id, _| live.contains(id));
            self.errors.retain(|id, _| live.contains(id));
            for (id, seen) in &frame {
                // `{cst}` / `{cst_token}`: the Captain Search daemon, started on first use (cst.rs).
                if seen.spec.url.contains("{cst") && crate::cst::state() == crate::cst::State::Idle {
                    crate::cst::ensure(|| crate::mods::request(crate::mods::Request::Refresh));
                }
                let url = match crate::cst::fill(&seen.spec.url, &crate::cst::state()) {
                    crate::cst::Url::Ready(u) => u,
                    crate::cst::Url::Waiting(m) | crate::cst::Url::Failed(m) => {
                        self.errors.insert(id.clone(), m);
                        if let Some(v) = self.views.get_mut(id) {
                            v.show(false);
                        }
                        continue;
                    }
                };
                if let Err(e) = allowed(&url, hosts) {
                    self.views.remove(id);
                    self.errors.insert(id.clone(), e);
                    continue;
                }
                if !self.views.contains_key(id) {
                    match imp::View::new(window, id, &url, seen.bounds, &keys, self.keys.clone(), hosts) {
                        Ok(v) => {
                            self.errors.remove(id);
                            self.views.insert(id.clone(), v);
                        }
                        Err(e) => {
                            self.errors.insert(id.clone(), e);
                            continue;
                        }
                    }
                }
                let view = self.views.get_mut(id).expect("just made");
                self.errors.remove(id);
                view.load(&url, hosts);
                view.zoom(zoom);
                if keys != self.sent_keys {
                    view.set_keys(&keys);
                }
                if covered(&seen.bounds, covers) {
                    view.show(false);
                } else {
                    view.place(seen.bounds);
                    view.show(true);
                }
            }
            for (id, view) in &mut self.views {
                if !frame.contains_key(id) {
                    view.show(false);
                }
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (window, covers, zoom, hosts);
            for id in frame.keys() {
                self.errors.insert(id.clone(), "web views need macOS in this build".into());
            }
            self.errors.retain(|id, _| live.contains(id));
        }
        self.sent_keys = keys;
    }

    pub fn reload(&self, id: &str) -> Result<(), String> {
        #[cfg(target_os = "macos")]
        return self.views.get(id).ok_or_else(|| format!("no web view {id:?}"))?.reload();
        #[cfg(not(target_os = "macos"))]
        Err(format!("no web view {id:?}"))
    }

    pub fn devtools(&self, id: &str) -> Result<(), String> {
        #[cfg(target_os = "macos")]
        return self.views.get(id).ok_or_else(|| format!("no web view {id:?}")).map(|v| v.devtools());
        #[cfg(not(target_os = "macos"))]
        Err(format!("no web view {id:?}"))
    }

    /// Give the keyboard back to the window (after a key the page handed back).
    pub fn return_focus(&self, id: &str) {
        #[cfg(target_os = "macos")]
        if let Some(v) = self.views.get(id) {
            v.return_focus();
        }
        #[cfg(not(target_os = "macos"))]
        let _ = id;
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;
    use wry::dpi::{LogicalPosition, LogicalSize};
    use wry::{NewWindowResponse, Rect, WebView, WebViewBuilder};

    pub struct View {
        web: WebView,
        url: String,
        shown: bool,
        placed: Option<Bounds<Pixels>>,
        zoom: f32,
        hosts: Rc<RefCell<Vec<String>>>,
    }

    fn rect(b: Bounds<Pixels>) -> Rect {
        Rect {
            position: LogicalPosition::new(b.origin.x.as_f32() as f64, b.origin.y.as_f32() as f64).into(),
            size: LogicalSize::new(b.size.width.as_f32().max(0.0) as f64, b.size.height.as_f32().max(0.0) as f64).into(),
        }
    }

    impl View {
        pub fn new(window: &gpui::Window, id: &str, url: &str, bounds: Bounds<Pixels>, keys: &[String], allowed_keys: Rc<RefCell<HashSet<String>>>, hosts: &[String]) -> Result<Self, String> {
            let hosts = Rc::new(RefCell::new(hosts.to_vec()));
            let nav_hosts = hosts.clone();
            let view_id = id.to_owned();
            let web = WebViewBuilder::new()
                .with_url(url)
                .with_bounds(rect(bounds))
                .with_visible(false)
                .with_devtools(true)
                .with_accept_first_mouse(true)
                .with_initialization_script(page_script(keys))
                .with_navigation_handler(move |to| {
                    let ok = allowed(&to, &nav_hosts.borrow()).is_ok();
                    if !ok {
                        tracing::warn!("web view refused to navigate to {to}");
                    }
                    ok
                })
                .with_new_window_req_handler(|to, _| {
                    tracing::warn!("web view refused a new window for {to}");
                    NewWindowResponse::Deny
                })
                .with_ipc_handler(move |req| {
                    let Ok(msg) = serde_json::from_str::<serde_json::Value>(req.body()) else { return };
                    if let Some((name, args)) = msg.get("captain").and_then(page_message) {
                        crate::mods::request(crate::mods::Request::Run { name, args });
                        return;
                    }
                    let Some(key) = msg.get("key").and_then(|k| k.as_str()) else { return };
                    // Only keys the window bound: a page can't press anything else.
                    if allowed_keys.borrow().contains(key) {
                        crate::mods::request(crate::mods::Request::Keystroke { keys: key.to_owned(), webview: view_id.clone() });
                    }
                })
                .build_as_child(window)
                .map_err(|e| format!("cannot open a web view: {e}"))?;
            Ok(View { web, url: url.to_owned(), shown: false, placed: Some(bounds), zoom: 1.0, hosts })
        }

        /// Follow a layout edit that changed the URL.
        pub fn load(&mut self, url: &str, hosts: &[String]) {
            if *self.hosts.borrow() != hosts {
                *self.hosts.borrow_mut() = hosts.to_vec();
            }
            if self.url != url {
                if let Err(e) = self.web.load_url(url) {
                    tracing::warn!("web view load {url}: {e}");
                }
                self.url = url.to_owned();
            }
        }

        pub fn place(&mut self, b: Bounds<Pixels>) {
            if self.placed != Some(b) {
                if let Err(e) = self.web.set_bounds(rect(b)) {
                    tracing::warn!("web view bounds: {e}");
                }
                self.placed = Some(b);
            }
        }

        pub fn show(&mut self, shown: bool) {
            if self.shown != shown {
                let _ = self.web.set_visible(shown);
                if !shown {
                    // A hidden view must not keep the keyboard.
                    let _ = self.web.focus_parent();
                }
                self.shown = shown;
            }
        }

        pub fn zoom(&mut self, zoom: f32) {
            if (self.zoom - zoom).abs() > f32::EPSILON {
                let _ = self.web.zoom(zoom as f64);
                self.zoom = zoom;
            }
        }

        pub fn set_keys(&self, keys: &[String]) {
            let list = serde_json::to_string(keys).unwrap_or_else(|_| "[]".into());
            let _ = self.web.evaluate_script(&format!("window.__captainKeys && window.__captainKeys({list})"));
        }

        pub fn reload(&self) -> Result<(), String> {
            self.web.reload().map_err(|e| e.to_string())
        }

        pub fn devtools(&self) {
            self.web.open_devtools();
        }

        pub fn return_focus(&self) {
            let _ = self.web.focus_parent();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gpui::{point, px, size};

    #[test]
    fn only_localhost_unless_the_person_allows_a_host() {
        for ok in ["http://localhost:3000/ui", "https://127.0.0.1/", "http://[::1]:8080/x", "http://LOCALHOST/", "about:blank"] {
            assert!(allowed(ok, &[]).is_ok(), "{ok}");
        }
        for bad in [
            "https://example.com/",
            "http://localhost.evil.com/",
            "http://localhost@evil.com/",
            "http://user:pw@localhost/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://127.0.0.2/",
            "not a url",
        ] {
            assert!(allowed(bad, &[]).is_err(), "{bad}");
        }
        assert!(allowed("https://app.captain.dev/x", &["app.captain.dev".into()]).is_ok());
        assert!(allowed("https://evil.dev/", &["app.captain.dev".into()]).is_err());
    }

    #[test]
    fn keys_have_one_form() {
        assert_eq!(canon("ctrl-alt-shift-escape").as_deref(), Some("ctrl-alt-shift-escape"));
        assert_eq!(canon("shift-alt-ctrl-escape").as_deref(), Some("ctrl-alt-shift-escape"));
        assert_eq!(canon("cmd-shift-t").as_deref(), Some("shift-cmd-t"));
        assert_eq!(canon("cmd-\\").as_deref(), Some("cmd-\\"));
        assert_eq!(canon("cmd--").as_deref(), Some("cmd--"));
        assert_eq!(canon("Cmd-K").as_deref(), Some("cmd-k"));
        assert_eq!(canon("ctrl-a b"), None);
        assert_eq!(canon(""), None);
    }

    #[test]
    fn the_hatch_and_window_keys_are_handed_back_but_not_editing_keys() {
        let l = |s: &str, k: &str, a: &str| (s.to_owned(), k.to_owned(), a.to_owned());
        let listing = vec![
            l("global", "cmd-k", "app.palette"),
            l("global", "cmd-c", "terminal.copy"),
            l("global", "cmd-1", "layout.select:main/explore"),
            l("layout:captain", "cmd-shift-p", "app.palette"),
            l("layout:other", "cmd-j", "app.palette"),
            l("terminal", "cmd-t", "tab.new"),
            l("global", "ctrl-l", "terminal"),
        ];
        let keys = forwarded_keys(&listing, "captain");
        assert_eq!(keys, vec!["cmd-1", "cmd-k", "ctrl-alt-shift-escape", "shift-cmd-p"]);
        // Even with nothing bound, the hatch always comes back.
        assert_eq!(forwarded_keys(&[], "main"), vec!["ctrl-alt-shift-escape"]);
    }

    #[test]
    fn covered_by_anything_that_overlaps() {
        let b = |x: f32, y: f32, w: f32, h: f32| Bounds { origin: point(px(x), px(y)), size: size(px(w), px(h)) };
        let view = b(200.0, 40.0, 800.0, 600.0);
        assert!(!covered(&view, &[]));
        assert!(covered(&view, &[b(400.0, 200.0, 300.0, 200.0)]));
        assert!(!covered(&view, &[b(0.0, 0.0, 190.0, 900.0)]));
        assert!(!covered(&view, &[b(400.0, 200.0, 0.0, 0.0)]));
    }

    #[test]
    fn the_page_script_lists_its_keys() {
        let s = page_script(&["cmd-k".into(), "ctrl-alt-shift-escape".into()]);
        assert!(s.contains(r#"new Set(["cmd-k","ctrl-alt-shift-escape"])"#), "{s}");
        assert!(s.contains("window.ipc.postMessage"));
    }

    #[test]
    fn a_page_can_switch_sections_and_nothing_else() {
        use serde_json::json;
        assert_eq!(page_message(&json!({"type": "captain:navigate", "screen": "runs", "tab": "x"})), Some(("layout.select".into(), json!({"stack": "sections", "child": "runs"}))));
        assert_eq!(page_message(&json!({"type": "captain:navigate", "screen": "settings"})), None);
        assert_eq!(page_message(&json!({"type": "captain:run", "screen": "runs"})), None);
        assert!(page_script(&[]).contains("messageHandlers"));
    }

    #[test]
    fn a_view_goes_by_its_id_or_its_block() {
        let spec = |id: Option<&str>| WebviewSpec { url: "http://localhost/".into(), id: id.map(String::from) };
        assert_eq!(view_id("b1", &spec(Some("explore"))), "explore");
        assert_eq!(view_id("b1", &spec(None)), "b1");
        assert_eq!(view_id("b1", &spec(Some(""))), "b1");
    }
}
