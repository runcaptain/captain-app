//! Takes secrets out of a line before it is written. A log may be pasted into a bug report, so anything that looks
//! like a key or a token goes, even inside an error message, and a person's home folder is written as `~`.
//!
//! The services' log does the same in Python (backend/core/captain_search/applog.py). Both are checked against
//! tests/fixtures/redaction-cases.json, so the two never drift apart. Change a rule in both.

use regex::{Captures, Regex};
use std::sync::OnceLock;

pub const MARK: &str = "[redacted]";

struct Rules {
    userinfo: Regex,
    header: Regex,
    bearer: Regex,
    pair: Regex,
    code: Regex,
    aws: Regex,
    prefixed: Regex,
    run: Regex,
    email: Regex,
    home: Regex,
}

fn rules() -> &'static Rules {
    static RULES: OnceLock<Rules> = OnceLock::new();
    RULES.get_or_init(|| Rules {
        // https://name:password@host
        userinfo: Regex::new(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@").unwrap(),
        // Authorization: anything, to the end of the line
        header: Regex::new(r#"(?i)\b((?:proxy-)?authorization|(?:set-)?cookie)(["']?\s*[:=]\s*)[^\r\n]*"#).unwrap(),
        bearer: Regex::new(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}").unwrap(),
        // api_key=..., "token": "...", password: ...
        pair: Regex::new(r#"(?i)\b([a-z0-9_.-]*(?:key|token|secret|password|passwd|pwd|passphrase|credentials?|signature))(["']?\s*[:=]\s*)(\[redacted\]|"[^"]*"|'[^']*'|[^\s"'&,;)}\]]+)"#).unwrap(),
        // Sign-in codes: a named one of any shape, or a bare code= that looks like one (6 to 8 digits, ABCD-1234).
        code: Regex::new(r#"(?i)\b((?:user|device|auth|authorization|verification|sign_?in|login|one_time)_code|otp|code_verifier)(["']?\s*[:=]\s*["']?)[A-Za-z0-9-]{4,}|\b(code)(["']?\s*[:=]\s*["']?)(?:\d{6,8}|[A-Za-z0-9]{4}-[A-Za-z0-9]{4})\b"#).unwrap(),
        aws: Regex::new(r"\b(?:AKIA|ASIA|AIDA|AROA|ABIA|ACCA)[A-Z0-9]{16}\b").unwrap(),
        prefixed: Regex::new(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}").unwrap(),
        // Long runs: 32 or more characters of hex, base64 or base64url.
        run: Regex::new(r"[A-Za-z0-9_+=-]{32,}").unwrap(),
        email: Regex::new(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+").unwrap(),
        home: Regex::new(r#"(?:/Users|/home|(?i:[a-z]:\\Users))[/\\][^/\\\s:"']+"#).unwrap(),
    })
}

/// A long run is a secret when it is all hex (a hash, a key) or mixes digits with both cases (base64).
/// Long plain words and kebab-case names stay.
fn looks_secret(run: &str) -> bool {
    let hex = run.bytes().all(|b| b.is_ascii_hexdigit());
    let digit = run.bytes().any(|b| b.is_ascii_digit());
    let upper = run.bytes().any(|b| b.is_ascii_uppercase());
    let lower = run.bytes().any(|b| b.is_ascii_lowercase());
    hex || (digit && upper && lower)
}

/// `text` with secrets replaced by `[redacted]`, email addresses by `[email]`, and home folders by `~`.
/// `home` is this person's home folder, when known (it may sit somewhere the general rule misses).
pub fn redact(text: &str, home: Option<&str>) -> String {
    let r = rules();
    let mut s = text.to_owned();
    if let Some(home) = home.map(|h| h.trim_end_matches(['/', '\\'])).filter(|h| h.len() > 1) {
        s = s.replace(home, "~");
    }
    let s = r.home.replace_all(&s, "~");
    let s = r.userinfo.replace_all(&s, format!("${{1}}{MARK}@"));
    let s = r.header.replace_all(&s, format!("${{1}}${{2}}{MARK}"));
    let s = r.bearer.replace_all(&s, format!("${{1}} {MARK}"));
    let s = r.pair.replace_all(&s, format!("${{1}}${{2}}{MARK}"));
    let s = r.code.replace_all(&s, |c: &Captures| {
        let (name, sep) = if c.get(1).is_some() { (&c[1], &c[2]) } else { (&c[3], &c[4]) };
        format!("{name}{sep}{MARK}")
    });
    let s = r.aws.replace_all(&s, MARK);
    let s = r.prefixed.replace_all(&s, MARK);
    let s = r.run.replace_all(&s, |c: &Captures| if looks_secret(&c[0]) { MARK.to_owned() } else { c[0].to_owned() });
    r.email.replace_all(&s, "[email]").into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// tests/fixtures/redaction-cases.json: `[{"in": ..., "out": ...}]`, shared with the Python tests.
    #[test]
    fn secrets_never_reach_a_log_line() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/redaction-cases.json");
        let cases: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let cases = cases["cases"].as_array().unwrap();
        assert!(cases.len() >= 20);
        for case in cases {
            let (input, want) = (case["in"].as_str().unwrap(), case["out"].as_str().unwrap());
            assert_eq!(redact(input, None), want, "{input}");
            // Redacting twice changes nothing more.
            assert_eq!(redact(want, None), want, "twice: {input}");
        }
    }

    #[test]
    fn this_persons_home_is_written_as_a_tilde() {
        assert_eq!(redact("cannot read /Volumes/work/sam/notes.txt", Some("/Volumes/work/sam")), "cannot read ~/notes.txt");
        assert_eq!(redact("cannot read /Volumes/work/sam/notes.txt", Some("/Volumes/work/sam/")), "cannot read ~/notes.txt");
        assert_eq!(redact("at /", Some("/")), "at /");
    }
}
