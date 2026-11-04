//! `difflib.SequenceMatcher(None, a, b).ratio()`, ported line for line.
//!
//! Identity clustering compares names with `ratio() >= threshold`, so a match count one off
//! moves a name into another cluster. The port keeps CPython's search order (which longest
//! match wins a tie decides how the rest of the strings split), the "autojunk" rule that
//! ignores characters making up more than 1% of a 200+ character `b`, and the extension loops
//! that grow a match through those ignored characters. It works on chars, as Python's `str`
//! does, not on UTF-8 bytes.

use std::collections::HashMap;

/// `SequenceMatcher(None, a, b).ratio()`: twice the matched characters over the total.
pub fn ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let length = a.len() + b.len();
    if length == 0 {
        return 1.0;
    }
    let matches: usize = Matcher::new(&a, &b)
        .matching_blocks()
        .iter()
        .map(|m| m.2)
        .sum();
    2.0 * matches as f64 / length as f64
}

struct Matcher<'a> {
    a: &'a [char],
    b: &'a [char],
    /// Where each character of `b` occurs, ascending, minus the popular ones.
    b2j: HashMap<char, Vec<usize>>,
}

impl<'a> Matcher<'a> {
    fn new(a: &'a [char], b: &'a [char]) -> Self {
        let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
        for (i, &c) in b.iter().enumerate() {
            b2j.entry(c).or_default().push(i);
        }
        // `__chain_b`: with autojunk on and `b` 200 long or more, characters occurring more
        // than `len(b) // 100 + 1` times are left out of the index. They aren't "junk" (no
        // isjunk was given), so the extension below still grows matches through them.
        let n = b.len();
        if n >= 200 {
            let ntest = n / 100 + 1;
            b2j.retain(|_, idxs| idxs.len() <= ntest);
        }
        Self { a, b, b2j }
    }

    /// `find_longest_match(alo, ahi, blo, bhi)`: the earliest longest match, as
    /// `(i, j, size)`.
    fn longest_match(
        &self,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (a, b) = (self.a, self.b);
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len = HashMap::new();
            if let Some(js) = self.b2j.get(c) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j
                        .checked_sub(1)
                        .and_then(|p| j2len.get(&p))
                        .copied()
                        .unwrap_or(0)
                        + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        (besti, bestj, bestsize) = (i + 1 - k, j + 1 - k, k);
                    }
                }
            }
            j2len = newj2len;
        }
        // Nothing is junk (no isjunk), so of CPython's four extension loops only the two that
        // extend through non-junk can fire; popular characters count as non-junk.
        while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
            (besti, bestj, bestsize) = (besti - 1, bestj - 1, bestsize + 1);
        }
        while besti + bestsize < ahi
            && bestj + bestsize < bhi
            && a[besti + bestsize] == b[bestj + bestsize]
        {
            bestsize += 1;
        }
        (besti, bestj, bestsize)
    }

    /// `get_matching_blocks()` without the final merge of adjacent blocks, which changes
    /// neither the blocks' total size nor anything `ratio()` reads.
    fn matching_blocks(&self) -> Vec<(usize, usize, usize)> {
        let mut queue = vec![(0, self.a.len(), 0, self.b.len())];
        let mut blocks = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, k) = self.longest_match(alo, ahi, blo, bhi);
            if k > 0 {
                blocks.push((i, j, k));
                if alo < i && blo < j {
                    queue.push((alo, i, blo, j));
                }
                if i + k < ahi && j + k < bhi {
                    queue.push((i + k, ahi, j + k, bhi));
                }
            }
        }
        blocks
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_ratios() {
        // Values from CPython 3.13.
        assert_eq!(ratio("", ""), 1.0);
        assert_eq!(ratio("abc", ""), 0.0);
        assert_eq!(ratio("artist 1", "artist 11"), 16.0 / 17.0);
        assert_eq!(ratio("abcd", "bcde"), 0.75);
        // A tie for the longest match: which one wins decides how the rest splits.
        assert_eq!(ratio("qabxcd", "abycdf"), 2.0 * 4.0 / 12.0);
        assert_eq!(ratio("private thread", "private  thread"), 28.0 / 29.0);
    }

    #[test]
    fn autojunk_drops_popular_characters_from_a_long_b() {
        // 'a' is over 1% of a 200+ char b, so it's never found by the index, but the empty
        // match at (0, 0) still grows through it.
        let b = "a".repeat(250);
        assert_eq!(ratio("a", &b), 2.0 / 251.0);
        let b = format!("x{}", "a".repeat(250));
        assert_eq!(ratio("xa", &b), 2.0 * 2.0 / 253.0);
    }
}
