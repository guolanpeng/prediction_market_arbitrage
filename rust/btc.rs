use crate::decimal as d;
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

fn epoch(value: &Value) -> Result<i64> {
    Ok(
        chrono::DateTime::parse_from_rfc3339(value.as_str().context("Missing market timestamp")?)?
            .timestamp(),
    )
}

pub fn array(value: &Value) -> Result<Vec<Value>> {
    if let Some(text) = value.as_str() {
        Ok(serde_json::from_str(text)?)
    } else {
        Ok(value.as_array().context("Expected array")?.clone())
    }
}

pub fn validate_window(poly: &Value, category: &Value, start: i64) -> Result<Value> {
    let slug = format!("btc-updown-5m-{start}");
    let matches: Vec<&Value> = poly
        .as_array()
        .context("Invalid Gamma response")?
        .iter()
        .filter(|r| r["slug"] == slug)
        .collect();
    ensure!(
        matches.len() == 1 && category["slug"] == slug,
        "Missing or ambiguous BTC window"
    );
    let poly = matches[0];
    ensure!(
        epoch(&poly["eventStartTime"])? == start
            && epoch(&poly["endDate"])? == start + 300
            && epoch(&category["startsAt"])? == start
            && epoch(&category["endsAt"])? == start + 300,
        "Venue time windows do not match"
    );
    ensure!(
        poly["closed"] == false && poly["acceptingOrders"] == true && category["status"] == "OPEN",
        "Market is not accepting orders"
    );
    ensure!(
        array(&poly["outcomes"])? == vec![json!("Up"), json!("Down")],
        "Polymarket outcome orientation is not verified"
    );
    let markets: Vec<&Value> = category["markets"]
        .as_array()
        .context("Missing Predict markets")?
        .iter()
        .filter(|r| {
            r["categorySlug"] == slug
                && r["marketVariant"] == "CRYPTO_UP_DOWN"
                && r["tradingStatus"] == "OPEN"
        })
        .collect();
    ensure!(
        markets.len() == 1,
        "Predict.fun window missing or ambiguous"
    );
    let predict = markets[0];
    let outcomes = array(&predict["outcomes"])?;
    ensure!(
        outcomes.len() == 2
            && outcomes
                .iter()
                .any(|r| r["indexSet"] == 1 && r["name"] == "Up")
            && outcomes
                .iter()
                .any(|r| r["indexSet"] == 2 && r["name"] == "Down"),
        "Predict outcome orientation is not verified"
    );
    let variant = if predict["variantData"].is_object() {
        &predict["variantData"]
    } else {
        &category["variantData"]
    };
    ensure!(
        matches!(
            variant["priceFeedSymbol"].as_str(),
            Some("BTCUSD" | "BTCUSDT")
        ),
        "Asset is not verified as BTC"
    );
    let id = |r: &Value| -> Result<String> {
        let id = r["id"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| r["id"].to_string());
        ensure!(
            !id.is_empty() && id.bytes().all(|b| b.is_ascii_digit()),
            "Numeric native market ID required"
        );
        Ok(id)
    };
    Ok(
        json!({"polymarket": id(poly)?, "predictfun": id(predict)?, "polymarket_rule": poly["description"], "predictfun_rule": predict["description"], "start": start, "end": start+300}),
    )
}

pub fn entry_reason(
    window: &Value,
    result: &Value,
    timestamps: [i64; 2],
    now: i64,
) -> Result<Option<&'static str>> {
    if now >= (window["end"].as_i64().context("Invalid window end")? - 60) * 1000 {
        return Ok(Some("SIZE_OR_TIME_NOT_ELIGIBLE"));
    }
    if timestamps
        .iter()
        .any(|t| *t <= 0 || !(0..=15000).contains(&(now - *t)))
    {
        return Ok(Some("QUOTE_TIME_UNKNOWN_OR_STALE"));
    }
    if (timestamps[0] - timestamps[1]).abs() > 5000 {
        return Ok(Some("QUOTE_TIME_SKEW"));
    }
    let legs = result["legs"].as_array().context("Missing legs")?;
    ensure!(legs.len() == 2, "Invalid leg count");
    if legs.iter().any(|r| r["complete"] != true) {
        return Ok(Some("INSUFFICIENT_DEPTH"));
    }
    if legs.iter().any(|r| r["fee"].is_null()) {
        return Ok(Some("FEES_UNKNOWN"));
    }
    if d::amount(&legs[1]["cost"])? < d::one() {
        return Ok(Some("PREDICT_MINIMUM_1_USDT"));
    }
    if d::amount(&result["net_per_pair"])? <= d::zero() {
        return Ok(Some("NET_PER_PAIR_NOT_POSITIVE"));
    }
    if d::sub(&d::amount(&result["net"])?, &d::parse("0.02")?) <= d::one() {
        return Ok(Some("NET_TOTAL_NOT_ABOVE_1_AFTER_BUFFER"));
    }
    Ok(None)
}

pub fn settlement_factors(
    poly: &Value,
    predict: &Value,
    sides: &[Value],
    clob: &Value,
) -> Result<Option<[d::Decimal; 2]>> {
    if predict["status"] != "RESOLVED"
        || array(&poly["outcomes"])? != vec![json!("Up"), json!("Down")]
    {
        return Ok(None);
    }
    let factors = if poly["umaResolutionStatus"] == "resolved" {
        let prices = array(&poly["outcomePrices"])?;
        if prices.len() != 2 {
            return Ok(None);
        }
        [d::amount(&prices[0])?, d::amount(&prices[1])?]
    } else if clob["closed"] == true {
        let tokens = array(&clob["tokens"])?;
        if tokens.len() != 2 {
            return Ok(None);
        }
        let winner = |label: &str| {
            tokens
                .iter()
                .find(|r| r["outcome"] == label)
                .and_then(|r| r["winner"].as_bool())
        };
        match (winner("Up"), winner("Down")) {
            (Some(a), Some(b)) if a != b => {
                [d::Decimal::from(u8::from(a)), d::Decimal::from(u8::from(b))]
            }
            _ => return Ok(None),
        }
    } else {
        return Ok(None);
    };
    if factors.iter().any(|v| v < &d::zero() || v > &d::one())
        || d::add(&factors[0], &factors[1]) != d::one()
    {
        return Ok(None);
    }
    let outcomes = array(&predict["outcomes"])?;
    if outcomes.len() != 2 {
        return Ok(None);
    }
    let up = outcomes
        .iter()
        .find(|r| r["indexSet"] == 1 && r["name"] == "Up");
    let down = outcomes
        .iter()
        .find(|r| r["indexSet"] == 2 && r["name"] == "Down");
    let (up, down) = match (up, down) {
        (Some(a), Some(b)) => (a, b),
        _ => return Ok(None),
    };
    let p = match (up["status"].as_str(), down["status"].as_str()) {
        (Some("WON"), Some("LOST")) => [d::one(), d::zero()],
        (Some("LOST"), Some("WON")) => [d::zero(), d::one()],
        _ => return Ok(None),
    };
    ensure!(
        sides.len() == 2 && sides.iter().all(|s| s == "yes" || s == "no"),
        "Invalid sides"
    );
    Ok(Some([
        factors[usize::from(sides[0] == "no")].clone(),
        p[usize::from(sides[1] == "no")].clone(),
    ]))
}
