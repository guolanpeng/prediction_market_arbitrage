//! Executable depth and price-dependent fees for a Polymarket/Predict.fun pair.
use crate::decimal::{self as d, Decimal};
use anyhow::{Result, ensure};
use bigdecimal::RoundingMode;
use serde_json::{Value, json};

pub fn fill_cost(asks: &Value, quantity: &Decimal, config: &Value) -> Result<Value> {
    let mut remaining = quantity.clone();
    let mut cost = d::zero();
    let mut fee = if config.is_null() {
        None
    } else {
        Some(d::zero())
    };
    for level in asks
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("Invalid asks"))?
    {
        let (price, size) = if level.is_object() {
            (d::amount(&level["price"])?, d::amount(&level["size"])?)
        } else {
            (d::amount(&level[0])?, d::amount(&level[1])?)
        };
        if price <= d::zero() || price >= d::one() || size <= d::zero() {
            continue;
        }
        let take = size.min(remaining.clone());
        cost = d::add(&cost, &d::mul(&price, &take));
        if let Some(total) = &fee {
            let inverse = d::sub(&d::one(), &price);
            let basis = if config["model"] == "quadratic_theta" {
                d::mul(&price, &inverse)
            } else {
                price.min(inverse)
            };
            fee = Some(d::add(
                total,
                &d::mul(&d::mul(&d::amount(&config["rate"])?, &basis), &take),
            ));
        }
        remaining = d::sub(&remaining, &take);
        if remaining == d::zero() {
            break;
        }
    }
    let fee = fee
        .map(|value| -> Result<Value> {
            let places = d::amount(&config["places"])?;
            Ok(d::json(&value.with_scale_round(
                places.as_bigint_and_exponent().1,
                RoundingMode::HalfUp,
            )))
        })
        .transpose()?
        .unwrap_or(Value::Null);
    Ok(
        json!({"filled": d::json(&d::sub(quantity, &remaining)), "complete": remaining == d::zero(), "cost": d::json(&cost), "fee": fee}),
    )
}

pub fn calculate(input: &Value) -> Result<Value> {
    let quantity = d::amount(&input["quantity"])?;
    ensure!(quantity > d::zero(), "Quantity must be positive");
    let configs = input["configs"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("Expected two fee configurations"))?;
    ensure!(configs.len() == 2, "Expected two fee configurations");
    let legs = [
        fill_cost(&input["asks_a"], &quantity, &configs[0])?,
        fill_cost(&input["asks_b"], &quantity, &configs[1])?,
    ];
    let mut result =
        json!({"legs": legs, "quantity": d::json(&quantity), "net": null, "gross": null});
    if legs.iter().any(|leg| leg["complete"] != true) {
        result["state"] = json!("INSUFFICIENT_DEPTH");
        return Ok(result);
    }
    let gross = d::sub(
        &quantity,
        &d::add(&d::amount(&legs[0]["cost"])?, &d::amount(&legs[1]["cost"])?),
    );
    result["gross"] = d::json(&gross);
    if legs.iter().any(|leg| leg["fee"].is_null()) {
        result["state"] = json!("FEES_UNKNOWN");
        return Ok(result);
    }
    let net = d::sub(
        &gross,
        &d::add(&d::amount(&legs[0]["fee"])?, &d::amount(&legs[1]["fee"])?),
    );
    result["net"] = d::json(&net);
    result["net_per_pair"] = d::json(&d::div(&net, &quantity)?);
    result["state"] = json!(if net > d::zero() {
        "POSITIVE_QUOTE_EDGE"
    } else {
        "NO_NET_EDGE"
    });
    Ok(result)
}
