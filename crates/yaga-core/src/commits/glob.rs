//! Case-sensitive Python fnmatch semantics for ignored commit headers.

use regex::Regex;
use std::fmt::Write;

pub(super) fn matches(pattern: &str, header: &str) -> bool {
    if !pattern.contains(['?', '[', '*']) {
        return pattern == header;
    }
    let chars: Vec<_> = pattern.chars().collect();
    let mut expr = String::from(r"(?s)\A");
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '*' => {
                expr.push_str(".*");
                while chars.get(i + 1) == Some(&'*') {
                    i += 1;
                }
            }
            '?' => expr.push('.'),
            '[' => {
                let start = i + 1;
                let mut pos = start + usize::from(chars.get(start) == Some(&'!'));
                pos += usize::from(chars.get(pos) == Some(&']'));
                if let Some(end) = chars[pos..].iter().position(|ch| *ch == ']') {
                    i = pos + end;
                    append_class(&mut expr, &chars[start..i]);
                } else {
                    expr.push_str(r"\[");
                }
            }
            ch => expr.push_str(&regex::escape(&ch.to_string())),
        }
        i += 1;
    }
    expr.push_str(r"\z");
    Regex::new(&expr).is_ok_and(|re| re.is_match(header))
}

fn append_class(expr: &mut String, class: &[char]) {
    // Preserve the fnmatch ordering: remove descending ranges first, then
    // interpret a leading '!' in the remaining class as negation.
    let mut chunks = Vec::<Vec<char>>::new();
    let mut start = 0;
    let mut pos = 1 + usize::from(class[0] == '!');
    while pos < class.len() {
        if class[pos] == '-' {
            chunks.push(class[start..pos].to_vec());
            start = pos + 1;
            pos += 3;
        } else {
            pos += 1;
        }
    }
    if start < class.len() {
        chunks.push(class[start..].to_vec());
    } else if let Some(last) = chunks.last_mut() {
        last.push('-');
    }
    for index in (1..chunks.len()).rev() {
        if chunks[index - 1].last() > chunks[index].first() {
            let tail = chunks.remove(index);
            chunks[index - 1].pop();
            chunks[index - 1].extend(tail.into_iter().skip(1));
        }
    }
    let negated = chunks.first().and_then(|s| s.first()) == Some(&'!');
    let mut members = String::new();
    for (index, chunk) in chunks.iter().enumerate() {
        if index > 0 {
            members.push('-');
        }
        let skip = usize::from(negated && index == 0);
        for &c in chunk.iter().skip(skip) {
            write!(members, r"\x{{{:x}}}", c as u32).unwrap();
        }
    }
    if members.is_empty() {
        expr.push_str(if negated { "." } else { r"[\x00&&[^\x00]]" });
        return;
    }
    expr.push('[');
    if negated {
        expr.push('^');
    }
    expr.push_str(&members);
    expr.push(']');
}
