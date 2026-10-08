//! Decimal operations with Python's 64-digit, half-even calculation context.
//! JSON amounts stay strings; chain integers must not pass through this type.
use std::{num::NonZeroU64, str::FromStr};

use anyhow::{Result, bail, ensure};
use bigdecimal::{BigDecimal, RoundingMode};
use num_bigint::{BigInt, Sign};
use num_traits::{One, Zero};
use serde_json::Value;

pub type Decimal = BigDecimal;

pub fn parse(value: &str) -> Result<Decimal> {
    // Bound exponents before allocating a decimal. No binary float conversion.
    let result = Decimal::from_str(value.trim())?;
    let (_, scale) = result.as_bigint_and_exponent();
    ensure!(
        scale.unsigned_abs() <= 10000 && result.digits() <= 10000,
        "Decimal exceeds supported bounds"
    );
    Ok(result)
}

pub fn amount(value: &Value) -> Result<Decimal> {
    match value {
        Value::String(value) => parse(value),
        Value::Number(value) => parse(&value.to_string()),
        _ => bail!("Invalid decimal amount"),
    }
}

pub fn round(value: Decimal) -> Decimal {
    if value.digits() > 64 {
        value.with_precision_round(NonZeroU64::new(64).unwrap(), RoundingMode::HalfEven)
    } else {
        value
    }
}

pub fn add(a: &Decimal, b: &Decimal) -> Decimal {
    round(a + b)
}
pub fn sub(a: &Decimal, b: &Decimal) -> Decimal {
    round(a - b)
}
pub fn mul(a: &Decimal, b: &Decimal) -> Decimal {
    round(a * b)
}

pub fn div(a: &Decimal, b: &Decimal) -> Result<Decimal> {
    ensure!(!b.is_zero(), "Division by zero");
    // Divide integers at the target precision directly, avoiding double rounding
    // from BigDecimal's default division context.
    let (mut numerator, a_scale) = a.as_bigint_and_exponent();
    let (mut denominator, b_scale) = b.as_bigint_and_exponent();
    if numerator.is_zero() {
        return Ok(Decimal::zero());
    }
    let negative = numerator.sign() != denominator.sign();
    if numerator.sign() == Sign::Minus {
        numerator = -numerator;
    }
    if denominator.sign() == Sign::Minus {
        denominator = -denominator;
    }
    let mut exponent =
        numerator.to_str_radix(10).len() as i64 - denominator.to_str_radix(10).len() as i64;
    if exponent >= 0 {
        if numerator < &denominator * BigInt::from(10).pow(exponent as u32) {
            exponent -= 1;
        }
    } else if &numerator * BigInt::from(10).pow((-exponent) as u32) < denominator {
        exponent -= 1;
    }
    let shift = 63 - exponent;
    if shift >= 0 {
        numerator *= BigInt::from(10).pow(shift as u32);
    } else {
        denominator *= BigInt::from(10).pow((-shift) as u32);
    }
    let mut quotient = &numerator / &denominator;
    let remainder = numerator % &denominator;
    let twice = remainder * 2;
    if twice > denominator || (twice == denominator && &quotient % 2 != BigInt::zero()) {
        quotient += 1;
    }
    if negative {
        quotient = -quotient;
    }
    Ok(Decimal::new(quotient, shift + a_scale - b_scale).normalized())
}

pub fn text(value: &Decimal) -> String {
    value.to_plain_string()
}
pub fn json(value: &Decimal) -> Value {
    Value::String(text(value))
}
pub fn zero() -> Decimal {
    Decimal::zero()
}
pub fn one() -> Decimal {
    Decimal::one()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn division_matches_half_even_without_float() {
        assert_eq!(
            text(&div(&one(), &parse("3").unwrap()).unwrap()),
            format!("0.{}", "3".repeat(64))
        );
        assert_eq!(
            div(&parse("10").unwrap(), &parse("0.1").unwrap()).unwrap(),
            parse("100").unwrap()
        );
        assert!(div(&one(), &zero()).is_err());
    }
    #[test]
    fn large_values_and_invalid_amounts() {
        assert_eq!(parse(&"9".repeat(78)).unwrap().digits(), 78);
        for value in ["NaN", "Infinity", "bad", "1e10001"] {
            assert!(parse(value).is_err());
        }
    }
}
