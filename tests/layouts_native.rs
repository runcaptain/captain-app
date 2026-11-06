//! CAPTAIN: no shipped layout puts a web page in the window (Edgar: "it should be
//! entirely native"). The webview block code stays in the fork until it's deleted,
//! but nothing shipped may use it.

fn has_webview(v: &serde_json::Value) -> bool {
    match v {
        serde_json::Value::Object(o) => o.contains_key("webview") || o.values().any(has_webview),
        serde_json::Value::Array(a) => a.iter().any(has_webview),
        _ => false,
    }
}

#[test]
fn no_shipped_layout_has_a_webview_block() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("defaults/layouts");
    for e in std::fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.extension().and_then(|x| x.to_str()) != Some("json") {
            continue;
        }
        let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        assert!(!has_webview(&v), "{} has a webview block", p.display());
    }
}

#[test]
fn the_check_sees_a_webview() {
    assert!(has_webview(&serde_json::json!({"children": [{"content": {"webview": {"url": "x"}}}]})));
    assert!(!has_webview(&serde_json::json!({"children": [{"content": {"mod": "captain-investigations"}}]})));
}
