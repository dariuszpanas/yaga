//! ISO-8601 parsing shared by review evidence and publication gates.
use chrono::{NaiveDate, Weekday};
use regex::Regex;
use std::sync::LazyLock;

// The Python contract accepts ISO basic and extended dates, week dates, one
// separator scalar and offsets with optional minutes, seconds and fractions.
pub(crate) fn parse_time(raw: &str) -> Option<Option<i64>> {
    static DATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"^([0-9]{4})(-[0-9]{2}-[0-9]{2}|[0-9]{4}|-W[0-9]{2}(?:-[1-7])?|W[0-9]{2}[1-7]?)",
        )
        .unwrap()
    });
    let date = DATE.captures(raw)?;
    let year: i32 = date[1].parse().ok()?;
    if year == 0 {
        return None;
    }
    let digits = date[2].replace('-', "");
    let day = if let Some(week) = digits.strip_prefix('W') {
        let weekday = match week.get(2..).unwrap_or("") {
            "" | "1" => Weekday::Mon,
            "2" => Weekday::Tue,
            "3" => Weekday::Wed,
            "4" => Weekday::Thu,
            "5" => Weekday::Fri,
            "6" => Weekday::Sat,
            "7" => Weekday::Sun,
            _ => return None,
        };
        NaiveDate::from_isoywd_opt(year, week[..2].parse().ok()?, weekday)?
    } else {
        NaiveDate::from_ymd_opt(year, digits[..2].parse().ok()?, digits[2..].parse().ok()?)?
    };
    let suffix = &raw[date.get(0)?.end()..];
    if suffix.is_empty() {
        return Some(None);
    }
    let time = &suffix[suffix.chars().next()?.len_utf8()..];
    let boundary = time.find(['+', '-', 'Z']).unwrap_or(time.len());
    let (clock, zone) = time.split_at(boundary);
    let (h, m, s, us) = clock_fields(clock)?;
    let wall = day
        .and_hms_micro_opt(h, m, s, us)?
        .and_utc()
        .timestamp_micros();
    if zone.is_empty() {
        return Some(None);
    }
    if zone == "Z" {
        return Some(Some(wall));
    }
    let (h, m, s, us) = clock_fields(zone.get(1..)?)?;
    if h >= 24 || m >= 60 || s >= 60 {
        return None;
    }
    let seconds = i64::from(h) * 3600 + i64::from(m) * 60 + i64::from(s);
    let offset = seconds * 1_000_000 + i64::from(us);
    Some(Some(if zone.starts_with('+') {
        wall - offset
    } else if zone.starts_with('-') {
        wall + offset
    } else {
        return None;
    }))
}
fn clock_fields(text: &str) -> Option<(u32, u32, u32, u32)> {
    static CLOCK: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^([0-9]{2})(?:(?::([0-9]{2})(?::([0-9]{2}))?)|(?:([0-9]{2})([0-9]{2})?))?(?:[.,]([0-9]+))?$").unwrap()
    });
    let parts = CLOCK.captures(text)?;
    let number = |a, b| {
        parts
            .get(a)
            .or_else(|| parts.get(b))
            .map(|m| m.as_str().parse().unwrap())
            .unwrap_or(0)
    };
    let fraction = parts.get(6).map(|m| m.as_str()).unwrap_or("");
    let micros = fraction
        .bytes()
        .take(6)
        .chain(std::iter::repeat(b'0'))
        .take(6)
        .fold(0, |us, b| us * 10 + u32::from(b - b'0'));
    Some((number(1, 1), number(2, 4), number(3, 5), micros))
}
