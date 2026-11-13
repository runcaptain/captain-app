//! UTC times as text, without a date crate.

use std::time::{SystemTime, UNIX_EPOCH};

/// Seconds and milliseconds since 1970, now.
pub fn now() -> (u64, u32) {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    (d.as_secs(), d.subsec_millis())
}

/// Year, month, day, hour, minute, second of `secs` since 1970, in UTC.
fn parts(secs: u64) -> (i64, u32, u32, u32, u32, u32) {
    let days = (secs / 86_400) as i64;
    let rest = (secs % 86_400) as u32;
    // Days to a civil date (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day, rest / 3_600, rest % 3_600 / 60, rest % 60)
}

/// `2026-11-13T10:22:33.123Z`: how every log line starts.
pub fn iso(secs: u64, millis: u32) -> String {
    let (y, mo, d, h, mi, s) = parts(secs);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{millis:03}Z")
}

/// `20261113T102233Z`: for file names.
pub fn compact(secs: u64) -> String {
    let (y, mo, d, h, mi, s) = parts(secs);
    format!("{y:04}{mo:02}{d:02}T{h:02}{mi:02}{s:02}Z")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn times_are_written_in_utc() {
        assert_eq!(iso(0, 0), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso(951_782_400, 7), "2000-02-29T00:00:00.007Z");
        assert_eq!(iso(1_794_565_353, 123), "2026-11-13T10:22:33.123Z");
        assert_eq!(compact(1_794_565_353), "20261113T102233Z");
    }
}
