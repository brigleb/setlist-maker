//! JSON written the way Python's `json.dumps(v, ensure_ascii=False, separators=(",", ":"))`
//! writes it, so `replay`'s output can be compared with Python's byte for byte: floats in
//! `repr` form (`1e-05`, `100.0`), the same escapes, keys in insertion order.

use std::fmt::Write;

use hindsight_core::engine::py::float_repr;
use serde_json::Value;

pub fn to_string(v: &Value) -> String {
    let mut out = String::new();
    write_value(v, &mut out);
    out
}

fn write_value(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                write!(out, "{i}").unwrap();
            } else if let Some(u) = n.as_u64() {
                write!(out, "{u}").unwrap();
            } else {
                // serde_json never holds NaN or infinity.
                out.push_str(&float_repr(n.as_f64().unwrap_or(f64::NAN)));
            }
        }
        Value::String(s) => write_str(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(k, out);
                out.push(':');
                write_value(item, out);
            }
            out.push('}');
        }
    }
}

fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => write!(out, "\\u{:04x}", c as u32).unwrap(),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn writes_what_python_writes() {
        let v = json!({"t": 100.0, "skew": -3.2305717e-05, "n": 3, "s": "a\"\\\n\u{1}\u{7f}é", "x": null});
        assert_eq!(
            to_string(&v),
            "{\"t\":100.0,\"skew\":-3.2305717e-05,\"n\":3,\"s\":\"a\\\"\\\\\\n\\u0001\u{7f}é\",\"x\":null}"
        );
    }
}
