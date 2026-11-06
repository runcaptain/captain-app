use gpui::*;

actions!(herdr, [Quit, Hide, HideOthers, ShowAll, Settings, About]);

pub fn init(cx: &mut App) {
    cx.on_action(|_: &Quit, cx| cx.quit());
    cx.on_action(|_: &Hide, cx| cx.hide());
    cx.on_action(|_: &HideOthers, cx| cx.hide_other_apps());
    cx.on_action(|_: &ShowAll, cx| cx.unhide_other_apps());
    cx.on_action(|_: &Settings, _| crate::mods::request(crate::mods::Request::Run { name: "app.settings".into(), args: serde_json::Value::Null }));
    // CAPTAIN: the standard About panel. It reads the name, version and the
    // NSHumanReadableCopyright line (the Herdr notice) from Info.plist.
    cx.on_action(|_: &About, _| about_panel());
    let name = crate::brand::NAME;
    cx.set_menus([Menu::new(name).items([
        MenuItem::action(format!("About {name}"), About),
        MenuItem::separator(),
        MenuItem::action("Settings", Settings),
        MenuItem::separator(),
        MenuItem::os_submenu("Services", SystemMenuType::Services),
        MenuItem::separator(),
        MenuItem::action(format!("Hide {name}"), Hide),
        MenuItem::action("Hide Others", HideOthers),
        MenuItem::action("Show All", ShowAll),
        MenuItem::separator(),
        MenuItem::action(format!("Quit {name}"), Quit),
    ])]);
}

fn about_panel() {
    use objc2::runtime::AnyObject;
    use objc2::{class, msg_send};
    // SAFETY: AppKit on the main thread (menu actions run there); nil sender.
    unsafe {
        let app: *mut AnyObject = msg_send![class!(NSApplication), sharedApplication];
        let nil: *mut AnyObject = std::ptr::null_mut();
        let _: () = msg_send![app, orderFrontStandardAboutPanel: nil];
    }
}

pub fn action(name: &str) -> Option<Box<dyn Action>> {
    match name {
        "app.quit" => Some(Box::new(Quit)),
        "app.hide" => Some(Box::new(Hide)),
        "app.hide_others" => Some(Box::new(HideOthers)),
        "app.show_all" => Some(Box::new(ShowAll)),
        "app.settings" => Some(Box::new(Settings)),
        "app.about" => Some(Box::new(About)),
        _ => None,
    }
}
