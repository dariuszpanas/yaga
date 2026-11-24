//! Line-oriented Markdown with byte-preserving masks for spelling, HTML and hygiene.
use regex::Regex;
use std::{collections::HashMap, sync::LazyLock};

macro_rules! pattern {
    ($name:ident, $expression:literal) => {
        static $name: LazyLock<Regex> = LazyLock::new(|| Regex::new($expression).unwrap());
    };
}
pattern!(ATX, r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$");
pattern!(CLOSING, r"[ \t]+#+[ \t]*$");
pattern!(FENCE, r"^ {0,3}(`{3,}|~{3,})(.*)$");
pattern!(LIST, r"^(?:[-+*]|[0-9]{1,9}[.)])[ \t]+(.*)$");
pattern!(TASK, r"^\[([ xX])\](?:[ \t]+(.*)|[ \t]*)$");
pattern!(TICKS, r"`+");
pattern!(UNDERLINE, r"^ {0,3}(=+|-+)[ \t]*$");
pattern!(
    RULE,
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})$"
);
pattern!(
    CONTAINER,
    r"^ {0,3}(?:>|(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$))"
);
pattern!(
    HTML_BLOCK,
    r"(?i)^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|pre|script|search|section|style|summary|table|tbody|td|textarea|tfoot|th|thead|title|tr|track|ul)(?:[ \t/>]|$)"
);

#[derive(Debug)]
pub(super) struct Heading {
    pub level: usize,
    pub title: String,
    pub line: usize,
    pub end: usize,
}
#[derive(Default)]
pub(super) struct Line {
    pub visible: String,
    pub content: String,
    pub unchecked: bool,
    pub heading: Option<Heading>,
}
pub(super) struct Document {
    pub lines: Vec<Line>,
    pub issues: Vec<(usize, &'static str)>,
    pub markup: Vec<String>,
    pub tags: Vec<(usize, usize)>,
}
impl Document {
    pub fn headings(&self) -> impl Iterator<Item = &Heading> {
        self.lines.iter().filter_map(|l| l.heading.as_ref())
    }
}

pub fn normalize(text: &str) -> Result<String, String> {
    if text.len() > 65536 {
        return Err("description exceeds the hard 65536-byte limit".into());
    }
    for (index, line) in text.split_inclusive('\n').enumerate() {
        let content = line.strip_suffix("\r\n").unwrap_or(line);
        for c in content.chars() {
            if c.is_control() && !matches!(c, '\n' | '\t') {
                let detail = if c == '\r' {
                    "bare carriage return; use LF or CRLF"
                } else {
                    "unsupported control character"
                };
                return Err(format!(
                    "description contains U+{:04X} at line {}: {detail}",
                    c as u32,
                    index + 1
                ));
            }
        }
    }
    Ok(text.replace("\r\n", "\n"))
}

fn opening(raw: &str) -> Option<(u8, usize)> {
    let groups = FENCE.captures(raw)?;
    let marker = &groups[1];
    let byte = marker.as_bytes()[0];
    (byte != b'`' || !groups[2].contains('`')).then_some((byte, marker.len()))
}
fn inline_closers(lines: &[(usize, &str)]) -> HashMap<usize, usize> {
    let mut result = HashMap::new();
    let mut next = HashMap::new();
    for &(start, raw) in lines.iter().rev() {
        let boundary = raw.trim().is_empty()
            || ATX.is_match(raw)
            || opening(raw).is_some()
            || UNDERLINE.is_match(raw)
            || RULE.is_match(raw)
            || CONTAINER.is_match(raw)
            || HTML_BLOCK.is_match(raw)
            || ["<!--", "<?", "<!"]
                .iter()
                .any(|s| raw.trim_start_matches(' ').starts_with(s));
        if boundary {
            next.clear();
        }
        for run in TICKS.find_iter(raw).collect::<Vec<_>>().into_iter().rev() {
            if let Some(end) = next.insert(run.len(), start + run.end()) {
                result.insert(start + run.start(), end);
            }
        }
        if boundary {
            next.clear();
        }
    }
    result
}

fn masked(
    raw: &str,
    start: usize,
    number: usize,
    closers: &HashMap<usize, usize>,
    comment: &mut Option<usize>,
    inline_end: &mut usize,
) -> (String, String) {
    if comment.is_none() && start >= *inline_end && !raw.contains(['<', '`', '\\']) {
        return (raw.to_owned(), raw.to_owned());
    }
    let mut visible = String::with_capacity(raw.len());
    let mut markup = String::with_capacity(raw.len());
    let mut offset = 0;
    while offset < raw.len() {
        let rest = &raw[offset..];
        let (length, hide, hide_markup) = if start + offset < *inline_end {
            (rest.len().min(*inline_end - start - offset), false, true)
        } else if comment.is_some() || rest.starts_with("<!--") {
            if comment.is_none() {
                *comment = Some(number);
            }
            let length = match rest.find("-->") {
                Some(i) => {
                    *comment = None;
                    i + 3
                }
                None => rest.len(),
            };
            (length, true, true)
        } else if rest.starts_with('\\')
            && matches!(rest.as_bytes().get(1), Some(b'\\' | b'`' | b'<'))
        {
            (2, false, true)
        } else if rest.starts_with('`') {
            if let Some(end) = closers.get(&(start + offset)) {
                *inline_end = *end;
                continue;
            }
            (
                rest.bytes().take_while(|&b| b == b'`').count(),
                false,
                false,
            )
        } else {
            (rest.chars().next().unwrap().len_utf8(), false, false)
        };
        if hide {
            visible.extend(std::iter::repeat_n(' ', length))
        } else {
            visible.push_str(&rest[..length])
        }
        if hide_markup {
            markup.extend(std::iter::repeat_n(' ', length))
        } else {
            markup.push_str(&rest[..length])
        }
        offset += length;
    }
    (visible, markup)
}

pub(super) fn parse(text: &str) -> Document {
    let mut start = 0;
    let raw_lines: Vec<_> = text
        .split('\n')
        .map(|raw| {
            let pos = start;
            start += raw.len() + 1;
            (pos, raw)
        })
        .collect();
    let closers = if text.contains('`') {
        inline_closers(&raw_lines)
    } else {
        HashMap::new()
    };
    let mut doc = Document {
        lines: Vec::with_capacity(raw_lines.len()),
        issues: vec![],
        markup: Vec::with_capacity(raw_lines.len()),
        tags: vec![],
    };
    let mut comment = None;
    let mut fence = None;
    let mut inline_end = 0;
    let mut indented = false;
    let mut paragraph = false;
    let mut html_block = false;
    for (i, (start, raw)) in raw_lines.into_iter().enumerate() {
        let number = i + 1;
        let indent = raw
            .bytes()
            .take_while(|b| matches!(b, b' ' | b'\t'))
            .fold(0, |width, b| {
                if b == b'\t' {
                    width + 4 - width % 4
                } else {
                    width + 1
                }
            });
        indented = (raw.trim().is_empty() && indented)
            || (indent >= 4
                && (indented || !paragraph)
                && !html_block
                && comment.is_none()
                && fence.is_none());
        let hide = if let Some((byte, size, _)) = fence {
            if FENCE.captures(raw).is_some_and(|g| {
                g[1].as_bytes()[0] == byte && g[1].len() >= size && g[2].trim().is_empty()
            }) {
                fence = None;
            }
            true
        } else if indented {
            true
        } else if let Some((byte, size)) = opening(raw).filter(|_| comment.is_none()) {
            fence = Some((byte, size, number));
            true
        } else {
            false
        };
        let (visible, markup) = if hide {
            let blank = " ".repeat(raw.len());
            (blank.clone(), blank)
        } else {
            masked(raw, start, number, &closers, &mut comment, &mut inline_end)
        };
        doc.markup.push(markup);
        let mut line = Line::default();
        if visible.trim().is_empty() {
            paragraph = false;
            html_block = false
        } else if HTML_BLOCK.is_match(&visible) {
            html_block = true;
            paragraph = false
        } else {
            paragraph = !(html_block
                || CONTAINER.is_match(&visible)
                || (paragraph && UNDERLINE.is_match(&visible))
                || RULE.is_match(&visible));
        }
        if let Some(g) = ATX.captures(&visible) {
            paragraph = false;
            let title = CLOSING.replace(g.get(2).map_or("", |v| v.as_str()), "");
            let title = title.trim();
            line.heading = Some(Heading {
                level: g[1].len(),
                title: if title.bytes().all(|b| b == b'#') {
                    String::new()
                } else {
                    title.into()
                },
                line: number,
                end: number,
            });
        } else {
            let mut content = visible.trim();
            if let Some(g) = LIST.captures(content) {
                content = g.get(1).unwrap().as_str().trim();
                if let Some(task) = TASK.captures(content) {
                    line.unchecked = &task[1] == " ";
                    content = task.get(2).map_or("", |m| m.as_str()).trim();
                }
            }
            line.content = content.into();
        }
        line.visible = visible;
        doc.lines.push(line);
    }
    if let Some(n) = comment {
        doc.issues.push((n, "HTML comment is not closed"));
    }
    if let Some((_, _, n)) = fence {
        doc.issues.push((n, "fenced code block is not closed"));
    }
    setext(&mut doc.lines);
    doc.tags = html_headings(&doc.markup);
    doc
}

fn setext(lines: &mut [Line]) {
    let mut paragraph: Vec<usize> = Vec::new();
    let mut container = false;
    for i in 0..lines.len() {
        let raw = &lines[i].visible;
        if raw.trim().is_empty() || lines[i].heading.is_some() {
            paragraph.clear();
            container = false;
        } else if let Some(groups) = UNDERLINE.captures(raw) {
            if let Some(&first) = paragraph.first().filter(|_| !container) {
                let title = paragraph
                    .iter()
                    .map(|&j| lines[j].visible.trim())
                    .collect::<Vec<_>>()
                    .join(" ");
                let level = if groups[1].starts_with('=') { 1 } else { 2 };
                lines[first].heading = Some(Heading {
                    level,
                    title,
                    line: first + 1,
                    end: i + 1,
                });
                for &j in &paragraph {
                    lines[j].content.clear();
                }
                lines[i].content.clear();
            }
            paragraph.clear();
        } else if CONTAINER.is_match(raw) || HTML_BLOCK.is_match(raw) {
            paragraph.clear();
            container = true;
        } else if container || RULE.is_match(raw) {
            paragraph.clear();
        } else {
            paragraph.push(i);
        }
    }
}

// Scan each nonblank block as a single stream so quoted attribute values can span lines.
fn html_headings(lines: &[String]) -> Vec<(usize, usize)> {
    let mut tags = Vec::new();
    let mut line = 0;
    while line < lines.len() {
        if lines[line].trim().is_empty() {
            line += 1;
            continue;
        }
        let first = line;
        while line < lines.len() && !lines[line].trim().is_empty() {
            line += 1;
        }
        let block = lines[first..line].join("\n");
        let starts: Vec<_> = std::iter::once(0)
            .chain(block.match_indices('\n').map(|(i, _)| i + 1))
            .collect();
        let data = block.as_bytes();
        let mut pos = 0;
        while let Some(offset) = block[pos..].find('<') {
            let tag = pos + offset;
            let mut cursor = tag + 1;
            let closing = data.get(cursor) == Some(&b'/');
            cursor += usize::from(closing);
            let Some(&byte) = data.get(cursor) else { break };
            if byte == b'!' || byte == b'?' || (closing && !byte.is_ascii_alphabetic()) {
                pos = block[cursor..]
                    .find('>')
                    .map_or(block.len(), |j| cursor + j + 1);
                continue;
            }
            if !byte.is_ascii_alphabetic() {
                pos = cursor;
                continue;
            }
            let name_start = cursor;
            while cursor < data.len()
                && !matches!(data[cursor], b'/' | b'>')
                && !data[cursor].is_ascii_whitespace()
            {
                cursor += 1;
            }
            let name = &block[name_start..cursor];
            let Some(end) = tag_end(data, cursor) else {
                break;
            };
            if name.len() == 2
                && name.as_bytes()[0].eq_ignore_ascii_case(&b'h')
                && matches!(name.as_bytes()[1], b'1'..=b'6')
            {
                let row = starts.partition_point(|&v| v <= tag) - 1;
                tags.push((first + row + 1, tag - starts[row] + 1));
            }
            pos = end;
        }
    }
    tags
}

/// Locate the closing delimiter without treating a quote in an attribute name
/// or an unquoted value as the start of a string.
fn tag_end(data: &[u8], mut pos: usize) -> Option<usize> {
    loop {
        while data
            .get(pos)
            .is_some_and(|c| c.is_ascii_whitespace() || *c == b'/')
        {
            pos += 1;
        }
        if *data.get(pos)? == b'>' {
            return Some(pos + 1);
        }
        pos += 1;
        while data
            .get(pos)
            .is_some_and(|c| !c.is_ascii_whitespace() && !matches!(c, b'/' | b'=' | b'>'))
        {
            pos += 1;
        }
        while data.get(pos).is_some_and(u8::is_ascii_whitespace) {
            pos += 1;
        }
        if data.get(pos) != Some(&b'=') {
            continue;
        }
        pos += 1;
        while data.get(pos).is_some_and(u8::is_ascii_whitespace) {
            pos += 1;
        }
        let next = *data.get(pos)?;
        if matches!(next, b'\'' | b'"') {
            pos += 1;
            pos += data[pos..].iter().position(|&c| c == next)? + 1;
        } else {
            while data
                .get(pos)
                .is_some_and(|c| !c.is_ascii_whitespace() && *c != b'>')
            {
                pos += 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::html_headings;

    #[test]
    fn html_attribute_quotes_preserve_subsequent_heading_locations() {
        for tag in ["<h2 title=a\"b>", "<h2 odd'name>", "<h2 title='x > y'>"] {
            let markup = format!("{tag}text</h2>");
            assert_eq!(html_headings(&[markup]), [(1, 1), (1, tag.len() + 5)]);
        }
        assert!(
            html_headings(&["</! <h3>>".into(), "<x title='unfinished <h2>".into()]).is_empty()
        );
    }
}
