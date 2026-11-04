//! The bits of Python's arithmetic the engine has to copy exactly.
//!
//! Resume is replay, and M2's parity harness checks that Hindsight folds every saved run into
//! byte-for-byte the tracklist Python folded. So wherever Python's numbers differ from the
//! obvious Rust spelling, the engine uses these instead:
//!
//! - `round(x, n)` rounds the exact decimal value of the float, half to even, and reads the
//!   result back. Scaling by `10^n` and rounding is a different, and sometimes wrong, answer.
//! - `round(x)` is half to even: [`f64::round_ties_even`], never [`f64::round`].
//! - `max(a, b)` keeps the first argument unless the second is strictly greater, so it can
//!   tell `0.0` from `-0.0` where [`f64::max`] doesn't say which it returns.
//! - `statistics.median` sorts (stably) and averages the middle two.
//! - `repr(float)` is the shortest round-trip form with Python's own exponent rules.

use serde_json::Value;

/// Python's `round(x, ndigits)` for a float and `ndigits >= 0`.
pub fn round_to(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // Rust's fixed-precision formatting is exact and rounds ties to even, as CPython's
    // `_Py_dg_dtoa` mode 3 does; parsing back is CPython's `_Py_dg_strtod`.
    format!("{x:.ndigits$}").parse().unwrap_or(x)
}

/// Python's `max(a, b)`: the first unless the second is strictly greater.
pub fn max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)`: the first unless the second is strictly smaller.
pub fn min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's `max(xs)`: the first of the largest. `None` when empty (Python raises).
pub fn max_of(xs: impl IntoIterator<Item = f64>) -> Option<f64> {
    xs.into_iter().reduce(max)
}

/// Python's `min(xs)`: the first of the smallest.
pub fn min_of(xs: impl IntoIterator<Item = f64>) -> Option<f64> {
    xs.into_iter().reduce(min)
}

/// `statistics.median`. `None` when empty (Python raises).
pub fn median(xs: &[f64]) -> Option<f64> {
    if xs.is_empty() {
        return None;
    }
    let mut sorted = xs.to_vec();
    // Stable, like `sorted()`, so equal values (0.0 and -0.0) keep their order.
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = sorted.len();
    Some(if n % 2 == 1 {
        sorted[n / 2]
    } else {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    })
}

/// A JSON value Python would accept in `isinstance(v, (int, float))`, as a float. `true` is
/// an int there, so it counts as 1.
pub fn number(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

/// Python's `v or 0` for a value then compared as a number. A non-number Python would crash
/// on reads as 0.
pub fn number_or_zero(v: Option<&Value>) -> f64 {
    number(v).unwrap_or(0.0)
}

/// Python's `repr(float)`: the shortest string that reads back as `x`, in fixed notation
/// from `1e-4` up to below `1e16`, otherwise as `1.5e-05` / `1e+16`.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    // `{:e}` is the shortest round-trip digits too, as `d.ddde<exp>`.
    let s = format!("{x:e}");
    let (mantissa, exp) = s.split_once('e').expect("{:e} always has an exponent");
    let exp: i32 = exp.parse().expect("{:e}'s exponent is an integer");
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(m) => ("-", m),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    if (-4..16).contains(&exp) {
        // The decimal point goes after `exp + 1` digits.
        let point = exp + 1;
        if point <= 0 {
            format!(
                "{sign}0.{}{digits}",
                "0".repeat(point.unsigned_abs() as usize)
            )
        } else {
            let point = point as usize;
            if point >= digits.len() {
                format!("{sign}{digits}{}.0", "0".repeat(point - digits.len()))
            } else {
                format!("{sign}{}.{}", &digits[..point], &digits[point..])
            }
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let dot = if rest.is_empty() { "" } else { "." };
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{first}{dot}{rest}e{esign}{:02}", exp.unsigned_abs())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn round_to_is_exact_and_half_even() {
        // Exact binary ties go to even; 2.675 is really 2.67499999..., so it goes down.
        assert_eq!(round_to(0.25, 1), 0.2);
        assert_eq!(round_to(0.75, 1), 0.8);
        assert_eq!(round_to(1.25, 1), 1.2);
        assert_eq!(round_to(0.125, 2), 0.12);
        assert_eq!(round_to(2.675, 2), 2.67);
        assert_eq!(round_to(0.0625, 3), 0.062);
        assert_eq!(round_to(13475.25, 1), 13475.2);
        assert_eq!(round_to(-0.04, 1).to_bits(), (-0.0f64).to_bits());
        assert!(round_to(f64::NAN, 1).is_nan());
    }

    #[test]
    fn max_and_min_keep_the_first_of_equals() {
        assert_eq!(max(0.0, -0.0).to_bits(), 0.0f64.to_bits());
        assert_eq!(max(-0.0, 0.0).to_bits(), (-0.0f64).to_bits());
        assert_eq!(min(-0.0, 0.0).to_bits(), (-0.0f64).to_bits());
        assert_eq!(max_of([1.0, 3.0, 2.0]), Some(3.0));
        assert_eq!(min_of(Vec::new()), None);
    }

    #[test]
    fn median_averages_the_middle_two() {
        assert_eq!(median(&[3.0, 1.0, 2.0]), Some(2.0));
        assert_eq!(median(&[151.0, 150.0]), Some(150.5));
        assert_eq!(median(&[]), None);
    }

    #[test]
    fn number_counts_bools_as_ints() {
        assert_eq!(number(Some(&json!(true))), Some(1.0));
        assert_eq!(number(Some(&json!(3))), Some(3.0));
        assert_eq!(number(Some(&json!("3"))), None);
        assert_eq!(number_or_zero(None), 0.0);
        assert_eq!(number_or_zero(Some(&Value::Null)), 0.0);
    }

    #[test]
    fn float_repr_matches_python() {
        let cases = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (100.0, "100.0"),
            (0.1, "0.1"),
            (13475.2, "13475.2"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (-3.2305717e-05, "-3.2305717e-05"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1234567890123456.0, "1234567890123456.0"),
            (108.76374218699999, "108.76374218699999"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (f64::INFINITY, "inf"),
        ];
        for (x, want) in cases {
            assert_eq!(float_repr(x), want, "{x:e}");
        }
    }
}
