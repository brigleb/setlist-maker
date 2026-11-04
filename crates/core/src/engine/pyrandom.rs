//! Python's `random.Random(seed)` for an int seed, so the oracle-driven tests draw the very
//! numbers Python's tests drew and can pin Python's measured results, not just bounds.
//!
//! MT19937 seeded through `init_by_array` with the seed's 32-bit words, as CPython does;
//! `random()` builds 53 bits from two draws.

const N: usize = 624;
const M: usize = 397;

pub struct Random {
    mt: [u32; N],
    index: usize,
}

impl Random {
    pub fn new(seed: u64) -> Self {
        let key: Vec<u32> = if seed == 0 {
            vec![0]
        } else {
            let mut key = Vec::new();
            let mut n = seed;
            while n > 0 {
                key.push(n as u32);
                n >>= 32;
            }
            key
        };
        let mut r = Random {
            mt: [0; N],
            index: N,
        };
        r.init_genrand(19_650_218);
        let (mut i, mut j) = (1, 0);
        for _ in 0..N.max(key.len()) {
            let prev = r.mt[i - 1] ^ (r.mt[i - 1] >> 30);
            r.mt[i] = (r.mt[i] ^ prev.wrapping_mul(1_664_525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                r.mt[0] = r.mt[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
        }
        for _ in 0..N - 1 {
            let prev = r.mt[i - 1] ^ (r.mt[i - 1] >> 30);
            r.mt[i] = (r.mt[i] ^ prev.wrapping_mul(1_566_083_941)).wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                r.mt[0] = r.mt[N - 1];
                i = 1;
            }
        }
        r.mt[0] = 0x8000_0000;
        r
    }

    fn init_genrand(&mut self, s: u32) {
        self.mt[0] = s;
        for i in 1..N {
            let prev = self.mt[i - 1] ^ (self.mt[i - 1] >> 30);
            self.mt[i] = 1_812_433_253u32.wrapping_mul(prev).wrapping_add(i as u32);
        }
        self.index = N;
    }

    fn next_u32(&mut self) -> u32 {
        if self.index >= N {
            for k in 0..N {
                let y = (self.mt[k] & 0x8000_0000) | (self.mt[(k + 1) % N] & 0x7fff_ffff);
                let mag = if y & 1 == 1 { 0x9908_b0df } else { 0 };
                self.mt[k] = self.mt[(k + M) % N] ^ (y >> 1) ^ mag;
            }
            self.index = 0;
        }
        let mut y = self.mt[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^ (y >> 18)
    }

    /// `random()`: a float in [0, 1) with 53 random bits.
    pub fn random(&mut self) -> f64 {
        let a = self.next_u32() >> 5;
        let b = self.next_u32() >> 6;
        (f64::from(a) * 67_108_864.0 + f64::from(b)) * (1.0 / 9_007_199_254_740_992.0)
    }

    /// `uniform(a, b)`.
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }

    /// `getrandbits(k)` for `k <= 32`.
    fn getrandbits(&mut self, k: u32) -> u32 {
        if k == 0 {
            0
        } else {
            self.next_u32() >> (32 - k)
        }
    }

    /// `choice(seq)`, by rejection sampling on `getrandbits`.
    pub fn choice<'a, T>(&mut self, seq: &'a [T]) -> &'a T {
        let n = seq.len() as u32;
        let k = 32 - n.leading_zeros();
        loop {
            let r = self.getrandbits(k);
            if r < n {
                return &seq[r as usize];
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn draws_what_cpython_draws() {
        let mut r = Random::new(7);
        assert_eq!(
            [r.random(), r.random(), r.random()],
            [0.32383276483316237, 0.15084917392450192, 0.6509344730398537]
        );
        let mut r = Random::new(0);
        assert_eq!(r.random(), 0.8444218515250481);
        assert_eq!(r.uniform(120.0, 14160.0), 10761.679817281847);
        assert_eq!(Random::new(700).random(), 0.16621443556684534);
        let mut r = Random::new(7);
        let picks: String = (0..12).map(|_| *r.choice(&['A', 'B'])).collect();
        assert_eq!(picks, "BABAAABAAAAB");
    }
}
