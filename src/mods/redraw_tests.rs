//! CAPTAIN: a mod's answers redraw that mod, in an idle window.
//!
//! Comparisons asked for its data after an `await` in `init`. Loading a mod ran
//! `init` but not the work it queued, so the next entry point to drain the
//! script's job queue ran it, often another mod's timer. The request, and the
//! tasks spawned from its answer, then belonged to that other mod, and their
//! `cx.notify()` redrew it. Comparisons sat on its skeleton until something
//! else redrew it (Edgar, 2026-11-06: 12 s and more after launch).
use gpui::{AppContext as _, Empty, TestAppContext, VisualTestContext};
use gpui_shell::{ShellRuntime, root::ShellRoot, view::ScriptView};
use std::path::PathBuf;

const TICKER: &str = r#"
import { div, View } from "gpui-kit";
export default class Ticker extends View {
  init(_props, cx) { cx.timer.every(10, (_cx) => {}); }
  render() { return div().child("ticker"); }
}
"#;

const DEFERRED: &str = r#"
import { div, View } from "gpui-kit";
export default class Deferred extends View {
  init(_props, cx) {
    this.state = "waiting";
    (async () => {
      await null;
      cx.spawn(async (cx) => { this.state = "loaded"; cx.notify(); });
    })();
  }
  render() { return div().child(`state: ${this.state}`); }
}
"#;

fn mod_dir(name: &str, source: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("captain-redraw-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("main.js"), source).unwrap();
    dir
}

fn view_of(root: &gpui::Entity<ShellRoot>, cx: &gpui::App) -> gpui::Entity<ScriptView> {
    root.read(cx).content().clone().downcast::<ScriptView>().expect("a script view")
}

#[gpui::test]
fn work_init_queues_redraws_its_own_mod(cx: &mut TestAppContext) {
    cx.update(gpui_shell::init);
    let runtime = cx.update(|cx| ShellRuntime::new(cx)).expect("runtime");
    let window = cx.add_window(|_, _| Empty);
    let mut context = VisualTestContext::from_window(*window, cx);
    let ticker_dir = mod_dir("ticker", TICKER);
    let deferred_dir = mod_dir("deferred", DEFERRED);
    let ticker = context.update(|window, cx| runtime.try_load(&ticker_dir, window, cx)).expect("load ticker");
    let deferred = context.update(|window, cx| runtime.try_load(&deferred_dir, window, cx)).expect("load deferred");
    let (ticker_view, view) = context.update(|_, cx| (view_of(&ticker, cx), view_of(&deferred, cx)));

    // Both drawn once, then an idle window: only the ticker's timer enters the script.
    for v in [&ticker_view, &view] {
        let v = v.clone();
        context.draw(gpui::Point::default(), gpui::size(gpui::px(400.), gpui::px(300.)), move |_, _| gpui::IntoElement::into_any_element(v));
    }
    context.executor().advance_clock(std::time::Duration::from_millis(50));
    context.run_until_parked();

    // The window draws only what was invalidated: draw it again and read what this mod shows.
    let v = view.clone();
    context.draw(gpui::Point::default(), gpui::size(gpui::px(400.), gpui::px(300.)), move |_, _| gpui::IntoElement::into_any_element(v));
    let shown = context.update(|_, cx| view.read(cx).snapshot().map(|s| s.debug_tree()).unwrap_or_default());
    assert!(shown.contains("state: loaded"), "the answer never reached the screen: {shown}");
    let _ = std::fs::remove_dir_all(ticker_dir);
    let _ = std::fs::remove_dir_all(deferred_dir);
}

// CAPTAIN: an answer reaches whoever awaits it after the screen that asked for it closes. All mods share one
// script runtime, so one screen's code can await a request another screen started. desktop-app saw a Comparisons
// GET stay pending for 20 s in 1 of 4 real-window runs (before 26b7cffb). This guards that path. It passes
// without a change: the stall hasn't been reproduced here.
const STARTER: &str = r#"
import { div, View } from "gpui-kit";
export default class Starter extends View {
  init(_props, cx) { globalThis.__captainShared = fetch("http://127.0.0.1:PORT/x"); }
  render() { return div().child("starter"); }
}
"#;

const WAITER: &str = r#"
import { div, View } from "gpui-kit";
export default class Waiter extends View {
  init(_props, cx) {
    this.state = "waiting";
    const pending = globalThis.__captainShared;
    cx.spawn(async (cx) => {
      try { const r = await pending; this.state = `answered ${r.status}`; }
      catch (e) { this.state = `failed ${e}`; }
      cx.notify();
    });
  }
  render() { return div().child(`state: ${this.state}`); }
}
"#;

#[gpui::test]
fn an_answer_reaches_its_waiter_after_the_screen_that_asked_closes(cx: &mut TestAppContext) {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        if let Ok((mut s, _)) = listener.accept() {
            let mut buf = [0u8; 1024];
            let _ = s.read(&mut buf);
            std::thread::sleep(std::time::Duration::from_millis(300));
            let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok");
        }
    });
    cx.update(gpui_shell::init);
    gpui_shell::set_capabilities(gpui_shell::Capabilities::new().network(gpui_shell::NetworkGrant::Unrestricted));
    let runtime = cx.update(|cx| ShellRuntime::new(cx)).expect("runtime");
    let window = cx.add_window(|_, _| Empty);
    let mut context = VisualTestContext::from_window(*window, cx);
    let starter_dir = mod_dir("starter", &STARTER.replace("PORT", &port.to_string()));
    let waiter_dir = mod_dir("waiter", WAITER);
    let starter = context.update(|window, cx| runtime.try_load(&starter_dir, window, cx)).expect("load starter");
    let waiter = context.update(|window, cx| runtime.try_load(&waiter_dir, window, cx)).expect("load waiter");
    let view = context.update(|_, cx| view_of(&waiter, cx));
    let asked = context.update(|_, cx| view_of(&starter, cx).downgrade());

    // The screen that asked closes before the answer comes (a layout switch builds new mods).
    drop(starter);
    context.run_until_parked();
    assert!(asked.upgrade().is_none(), "the screen that asked must be closed for this to test anything");

    let mut shown = String::new();
    for _ in 0..60 {
        std::thread::sleep(std::time::Duration::from_millis(50));
        context.run_until_parked();
        let v = view.clone();
        context.draw(gpui::Point::default(), gpui::size(gpui::px(400.), gpui::px(300.)), move |_, _| gpui::IntoElement::into_any_element(v));
        shown = context.update(|_, cx| view.read(cx).snapshot().map(|s| s.debug_tree()).unwrap_or_default());
        if !shown.contains("waiting") {
            break;
        }
    }
    let _ = std::fs::remove_dir_all(starter_dir);
    let _ = std::fs::remove_dir_all(waiter_dir);
    assert!(shown.contains("answered 200"), "the waiter never heard the answer: {shown}");
}
