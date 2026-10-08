//! BTC paired cash orders, reconciliation, one-shot imbalance exits and settlement.
use crate::{
    adapters::{self, Clients, Markets, Receipt},
    btc,
    decimal::{self as d, Decimal},
    journal::Journal,
    signing,
};
use alloy_primitives::U256;
use anyhow::{Context, Result, ensure};
use bigdecimal::RoundingMode;
use reqwest::Method;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    str::FromStr,
    time::{Duration, Instant},
};

pub fn plans(
    candidate: &Value,
    quantity: &Decimal,
    token: &Value,
    window: &Value,
) -> Result<[Value; 2]> {
    ensure!(
        quantity > &d::zero() && quantity <= &Decimal::from(10),
        "Live target shares must be >0 and <=10"
    );
    let result = &candidate["result"];
    let sides = candidate["sides"].as_array().context("Missing sides")?;
    ensure!(
        sides.len() == 2 && sides.iter().all(|s| s == "yes" || s == "no") && sides[0] != sides[1],
        "Opposite directions required"
    );
    let tick = d::amount(&token["tick"])?;
    ensure!(tick > d::zero() && tick <= d::parse("0.5")?, "Invalid tick");
    let predict_tick = d::amount(&candidate["ticks"][1])?;
    ensure!(
        predict_tick > d::zero() && predict_tick <= d::parse("0.5")?,
        "Unknown or invalid Predict tick size"
    );
    let poly_budget =
        d::amount(&result["legs"][0]["cost"])?.with_scale_round(2, RoundingMode::Down);
    let predict_quoted =
        d::amount(&result["legs"][1]["cost"])?.with_scale_round(18, RoundingMode::Down);
    ensure!(
        poly_budget >= d::one() && predict_quoted >= d::one(),
        "Market budget below venue minimum"
    );
    let target = signing::significant(signing::raw(quantity, 18)?, 5);
    ensure!(
        target >= U256::from(10).pow(U256::from(16)),
        "Predict shares below minimum"
    );
    let mut remaining = target;
    let mut processed = U256::ZERO;
    let mut total = U256::ZERO;
    let mut last = U256::ZERO;
    for row in candidate["asks"][1]
        .as_array()
        .context("Missing Predict depth")?
    {
        let price = d::amount(&row[0])?;
        let size = d::amount(&row[1])?;
        if price <= d::zero() || price >= d::one() || size <= d::zero() {
            continue;
        }
        let price = signing::raw(&price, 18)?;
        let size = signing::raw(&size, 18)?.min(remaining);
        total = total
            .checked_add(price.checked_mul(size).context("Book cost overflow")?)
            .context("Book cost overflow")?;
        processed += size;
        remaining -= size;
        last = price;
        if remaining == U256::ZERO {
            break;
        }
    }
    ensure!(
        processed == target,
        "Insufficient Predict depth for target shares"
    );
    let scale = U256::from(10).pow(U256::from(18));
    let budget = (last * processed / scale * U256::from(10100) / U256::from(10000)).min(processed);
    ensure!(
        budget < processed,
        "Predict slippage budget per share must be below $1"
    );
    let expected = total / processed;
    Ok([
        json!({"venue":"polymarket","market_id":format!("polymarket:{}",window["polymarket"].as_str().context("Missing market")?),"outcome":sides[0],"target_shares":d::json(quantity),"cash_budget":d::json(&poly_budget),"expected_price":d::json(&d::div(&d::amount(&result["legs"][0]["cost"])?,quantity)?),"tick":d::json(&tick)}),
        json!({"venue":"predict_fun","market_id":format!("predict_fun:{}",window["predictfun"].as_str().context("Missing market")?),"outcome":sides[1],"target_shares":d::json(quantity),"cash_budget":d::json(&signing::from_raw(budget,18)?),"quoted_cash_budget":d::json(&predict_quoted),"expected_price":d::json(&signing::from_raw(expected,18)?),"worst_price":d::json(&signing::from_raw(last,18)?),"tick":candidate["ticks"][1],"slippage_bps":100}),
    ])
}

pub fn estimated_share_fee(asks: &Value, quantity: &Decimal, config: &Value) -> Result<Decimal> {
    let mut remaining = quantity.clone();
    let mut fee = d::zero();
    let rate = d::amount(&config["rate"])?;
    for row in asks.as_array().context("Missing depth")? {
        let price = d::amount(&row[0])?;
        let size = d::amount(&row[1])?;
        if price <= d::zero() || price >= d::one() || size <= d::zero() {
            continue;
        }
        let amount = size.min(remaining.clone());
        let basis = price.clone().min(d::sub(&d::one(), &price));
        fee = d::add(
            &fee,
            &d::div(&d::mul(&d::mul(&rate, &basis), &amount), &price)?,
        );
        remaining = d::sub(&remaining, &amount);
        if remaining == d::zero() {
            break;
        }
    }
    Ok(fee)
}

pub fn conservative(
    candidate: &Value,
    plans: &[Value; 2],
    quantity: &Decimal,
) -> Result<(Decimal, Decimal, [Decimal; 2])> {
    let gross = signing::from_raw(signing::significant(signing::raw(quantity, 18)?, 5), 18)?;
    let fee = estimated_share_fee(&candidate["asks"][1], &gross, &candidate["configs"][1])?;
    let projected = [
        d::div(
            &d::mul(quantity, &d::amount(&plans[0]["cash_budget"])?),
            &d::amount(&candidate["result"]["legs"][0]["cost"])?,
        )?,
        d::sub(&gross, &fee),
    ];
    let minimum = projected[0].clone().min(projected[1].clone());
    let net = d::sub(
        &d::sub(
            &d::sub(
                &minimum,
                &d::add(
                    &d::amount(&plans[0]["cash_budget"])?,
                    &d::amount(&plans[1]["cash_budget"])?,
                ),
            ),
            &d::amount(&candidate["result"]["legs"][0]["fee"])?,
        ),
        &d::parse("0.02")?,
    );
    Ok((net, fee, projected))
}

pub fn inventory(fills: &HashMap<String, Receipt>, reduce_only: bool) -> Result<Value> {
    let mut gross = d::zero();
    let mut share_fees = d::zero();
    let mut cash = d::zero();
    let mut cash_fee = Some(d::zero());
    for fill in fills.values().filter(|r| r.settlement == "CONFIRMED") {
        gross = d::add(&gross, &fill.amount);
        cash = d::add(&cash, &d::mul(&fill.amount, &fill.price));
        if fill.fee_currency.as_deref() == Some("shares") && !reduce_only {
            let fee = fill.fee.as_ref().context("Unknown share-denominated fee")?;
            ensure!(fee >= &d::zero(), "Negative fee");
            share_fees = d::add(&share_fees, fee);
        } else if fill.fee_currency.as_deref() != Some("shares") {
            cash_fee = match (cash_fee, fill.fee.as_ref()) {
                (Some(total), Some(fee)) => {
                    ensure!(fee >= &d::zero(), "Negative fee");
                    Some(d::add(&total, fee))
                }
                _ => None,
            };
        }
    }
    ensure!(share_fees <= gross, "Share fees exceed gross fill");
    Ok(
        json!({"gross_shares":d::json(&gross),"share_fees":d::json(&share_fees),"net_shares":d::json(&d::sub(&gross,&share_fees)),"cash_value":d::json(&cash),"cash_fee":cash_fee.as_ref().map(d::json)}),
    )
}

pub fn poly_receipts(
    raw: &Value,
    order: &Value,
    markets: &Markets,
    api_key: &str,
    wallet: &str,
    signer: &str,
) -> Result<Vec<Receipt>> {
    let mut result = Vec::new();
    let id = order["id"].as_str().context("Missing order ID")?;
    let status = raw["status"]
        .as_str()
        .unwrap_or("")
        .trim_start_matches("TRADE_STATUS_")
        .to_uppercase();
    let settlement = match status.as_str() {
        "CONFIRMED" => "CONFIRMED",
        "FAILED" => "FAILED",
        _ => "MATCHED",
    };
    let token = |asset: &Value| -> Result<()> {
        let token = asset.as_str().context("Missing receipt token")?;
        ensure!(
            markets.tokens.iter().any(|t| t == token),
            "Unknown receipt token"
        );
        let side = order["side"].as_str().context("Missing order side")?;
        let reducing = order["reduce_only"] == true;
        let yes = (side == "buy") != reducing;
        ensure!(
            token == markets.tokens[usize::from(!yes)],
            "Receipt outcome disagrees with order"
        );
        Ok(())
    };
    if raw["trader_side"]
        .as_str()
        .unwrap_or("")
        .eq_ignore_ascii_case("TAKER")
        && raw["taker_order_id"] == id
    {
        token(&raw["asset_id"])?;
        result.push(Receipt {
            id: raw["id"].as_str().context("Missing trade ID")?.into(),
            order_id: id.into(),
            amount: d::amount(&raw["size"])?,
            price: d::amount(&raw["price"])?,
            side: raw["side"]
                .as_str()
                .context("Missing fill side")?
                .to_uppercase(),
            settlement: settlement.into(),
            fee: None,
            fee_currency: Some("pUSD".into()),
        });
    } else {
        for leg in raw["maker_orders"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            let mine = leg["owner"] == api_key
                || leg["maker_address"].as_str().is_some_and(|s| {
                    s.eq_ignore_ascii_case(wallet) || s.eq_ignore_ascii_case(signer)
                });
            if !mine || leg["order_id"] != id {
                continue;
            }
            token(&leg["asset_id"])?;
            result.push(Receipt {
                id: format!("{}:{id}", raw["id"].as_str().context("Missing trade ID")?),
                order_id: id.into(),
                amount: d::amount(&leg["matched_amount"])?,
                price: d::amount(&leg["price"])?,
                side: leg["side"]
                    .as_str()
                    .context("Missing fill side")?
                    .to_uppercase(),
                settlement: settlement.into(),
                fee: Some(d::zero()),
                fee_currency: Some("pUSD".into()),
            });
        }
    }
    Ok(result)
}

pub fn predict_receipts(raw: &Value, order: &Value, address: &str) -> Result<Vec<Receipt>> {
    let mut result = Vec::new();
    let id = order["id"].as_str().context("Missing order ID")?;
    let mut legs = Vec::new();
    let taker = &raw["taker"];
    if taker["signer"]
        .as_str()
        .is_some_and(|s| s.eq_ignore_ascii_case(address))
    {
        legs.push((taker, true));
    }
    for leg in raw["makers"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        if leg["signer"]
            .as_str()
            .is_some_and(|s| s.eq_ignore_ascii_case(address))
        {
            legs.push((leg, false));
        }
    }
    for (leg, taker) in legs {
        if !leg["hash"]
            .as_str()
            .is_some_and(|s| s.eq_ignore_ascii_case(id))
        {
            continue;
        }
        let side = leg["quoteType"].as_str().context("Missing quote type")?;
        ensure!(
            side.eq_ignore_ascii_case("bid") || side.eq_ignore_ascii_case("ask"),
            "Invalid quote type"
        );
        let outcome = leg["outcome"]["indexSet"]
            .as_i64()
            .context("Missing fill outcome")?;
        ensure!(outcome == 1 || outcome == 2, "Invalid fill outcome");
        let yes = (order["side"] == "buy") != (order["reduce_only"] == true);
        ensure!(
            (outcome == 1) == yes,
            "Receipt outcome disagrees with order"
        );
        let fee = &leg["fee"];
        let has_fee = fee.is_object() && !fee.as_object().unwrap().is_empty();
        let currency = if fee["type"] == "COLLATERAL" {
            Some("USDT".into())
        } else if has_fee {
            Some("shares".into())
        } else {
            None
        };
        result.push(Receipt {
            id: raw["settlementId"]
                .as_str()
                .or(raw["transactionHash"].as_str())
                .context("Missing settlement ID")?
                .into(),
            order_id: id.into(),
            amount: adapters::shares(if taker {
                &raw["amountFilled"]
            } else {
                &leg["amount"]
            })?,
            price: adapters::shares(&leg["price"])?,
            side: if side.eq_ignore_ascii_case("bid") {
                "BUY"
            } else {
                "SELL"
            }
            .into(),
            settlement: "CONFIRMED".into(),
            fee: if !fee["amount"].is_null() && fee["amount"] != "" {
                Some(adapters::shares(&fee["amount"])?)
            } else {
                None
            },
            fee_currency: currency,
        });
    }
    Ok(result)
}

pub async fn confirmed(
    clients: &Clients,
    index: usize,
    order: &Value,
    markets: &Markets,
    ws: Vec<Receipt>,
) -> Result<Option<Value>> {
    if order == "REJECTED" {
        return Ok(Some(inventory(&HashMap::new(), false)?));
    }
    if order.is_null() {
        return Ok(None);
    }
    let id = order["id"].as_str().context("Missing order ID")?;
    let ws: Vec<Receipt> = ws
        .into_iter()
        .filter(|r| r.order_id.eq_ignore_ascii_case(id))
        .collect();
    let current = clients
        .private(
            index,
            Method::GET,
            &format!(
                "{}/{}",
                if index == 0 { "/data/order" } else { "/orders" },
                id
            ),
            &[],
            None,
        )
        .await?;
    let current = if index == 1 {
        &current["data"]
    } else {
        &current
    };
    if index == 1 {
        ensure!(
            !current["amountFilled"].is_null() && current["order"].is_object(),
            "Predict order fill state missing"
        );
        ensure!(
            current["order"]["hash"]
                .as_str()
                .is_some_and(|hash| hash.eq_ignore_ascii_case(id)),
            "Predict order identity disagrees"
        );
    } else {
        ensure!(
            current["id"]
                .as_str()
                .is_some_and(|hash| hash.eq_ignore_ascii_case(id)),
            "Polymarket order identity disagrees"
        );
    }
    let filled = if index == 0 {
        d::amount(&current["size_matched"])?
    } else {
        adapters::shares(&current["amountFilled"])?
    };
    ensure!(filled >= d::zero(), "Negative reported fill");
    let mut fills = Vec::new();
    let mut cursor = None;
    for _ in 0..5 {
        let mut params = if index == 0 {
            vec![
                (
                    "market".into(),
                    markets.poly["conditionId"]
                        .as_str()
                        .context("Missing condition")?
                        .into(),
                ),
                (
                    "next_cursor".into(),
                    cursor.clone().unwrap_or_else(|| "MA==".into()),
                ),
            ]
        } else {
            vec![
                ("first".into(), "100".into()),
                (
                    "signerAddress".into(),
                    clients.predict_address().to_checksum(None),
                ),
                (
                    "marketId".into(),
                    markets.window["predictfun"]
                        .as_str()
                        .context("Missing market")?
                        .into(),
                ),
                ("orderHashes".into(), id.into()),
            ]
        };
        if index == 1
            && let Some(cursor) = &cursor
        {
            params.push(("after".into(), cursor.clone()));
        }
        let raw = if index == 0 {
            clients
                .private(0, Method::GET, "/data/trades", &params, None)
                .await?
        } else {
            clients.data.predict("/orders/matches", &params).await?
        };
        for row in raw["data"].as_array().context("Invalid trade page")? {
            fills.extend(if index == 0 {
                poly_receipts(
                    row,
                    order,
                    markets,
                    clients.poly_api["apiKey"]
                        .as_str()
                        .context("Missing API key")?,
                    &clients.poly_maker.to_checksum(None),
                    &clients.poly_wallet.address.to_checksum(None),
                )?
            } else {
                predict_receipts(row, order, &clients.predict_address().to_checksum(None))?
            });
        }
        cursor = raw[if index == 0 { "next_cursor" } else { "cursor" }]
            .as_str()
            .filter(|s| !s.is_empty() && *s != "LTE=")
            .map(str::to_owned);
        if cursor.is_none() {
            break;
        }
    }
    if cursor.is_some() {
        return Ok(None);
    }
    // CLOB trade pagination can lag the order's direct trade IDs.
    if index == 0 {
        for trade_id in current["associate_trades"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[])
            .iter()
            .take(20)
        {
            let raw = clients
                .private(
                    0,
                    Method::GET,
                    "/data/trades",
                    &[
                        (
                            "market".into(),
                            markets.poly["conditionId"]
                                .as_str()
                                .context("Missing condition")?
                                .into(),
                        ),
                        (
                            "id".into(),
                            trade_id.as_str().context("Invalid trade ID")?.into(),
                        ),
                    ],
                    None,
                )
                .await?;
            for row in raw["data"]
                .as_array()
                .context("Invalid direct trade page")?
            {
                fills.extend(poly_receipts(
                    row,
                    order,
                    markets,
                    clients.poly_api["apiKey"]
                        .as_str()
                        .context("Missing API key")?,
                    &clients.poly_maker.to_checksum(None),
                    &clients.poly_wallet.address.to_checksum(None),
                )?);
            }
        }
    }
    let has_ws = !ws.is_empty();
    let merged = adapters::merge_receipts(fills, ws, index == 1)?;
    if merged.values().any(|r| r.settlement == "MATCHED") {
        return Ok(None);
    }
    let accounted = merged
        .values()
        .filter(|r| r.settlement == "CONFIRMED" || r.settlement == "FAILED")
        .fold(d::zero(), |total, r| d::add(&total, &r.amount));
    let status = current["status"].as_str().unwrap_or("").to_uppercase();
    let terminal = if index == 0 {
        matches!(
            status.as_str(),
            "CANCELED" | "CANCELLED" | "EXPIRED" | "REJECTED"
        ) || status == "MATCHED"
            && (current["order_type"] == "FAK" || filled >= d::amount(&current["original_size"])?)
    } else {
        matches!(
            status.as_str(),
            "FILLED" | "CANCELLED" | "CANCELED" | "INVALIDATED" | "EXPIRED" | "REJECTED"
        )
    };
    if !terminal
        || if has_ws {
            accounted != filled
        } else {
            accounted < filled
        }
    {
        return Ok(None);
    }
    Ok(Some(inventory(&merged, order["reduce_only"] == true)?))
}

fn safe_order(id: &str, index: usize, plan: &Value, amount: &Decimal, reducing: bool) -> Value {
    let outcome_yes = plan["outcome"] == "yes";
    let side = if outcome_yes != reducing {
        "buy"
    } else {
        "sell"
    };
    json!({"id":id,"venue":if index==0{"polymarket"}else{"predict_fun"},"market_id":plan["market_id"],"side":side,"status":"pending","amount":d::json(amount),"filled":"0","remaining":d::json(amount),"price":null,"reduce_only":reducing})
}

pub async fn execute(
    clients: &mut Clients,
    journal: &mut Journal,
    markets: &Markets,
    candidate: &Value,
    quantity: &Decimal,
    feed: &crate::feed::Feeds,
    attempt_limits: (u32, u32),
) -> Result<Option<Value>> {
    let start = markets.window["start"].as_i64().context("Invalid start")?;
    if journal.windows.contains(&start) {
        return Ok(None);
    }
    let timestamps = [
        candidate["timestamps"][0]
            .as_i64()
            .context("Missing timestamp")?,
        candidate["timestamps"][1]
            .as_i64()
            .context("Missing timestamp")?,
    ];
    if btc::entry_reason(
        &markets.window,
        &candidate["result"],
        timestamps,
        crate::now_ms(),
    )?
    .is_some()
    {
        return Ok(None);
    }
    let side = candidate["sides"][0].as_str().context("Missing side")?;
    let token = clients
        .data
        .token_info(&markets.tokens[usize::from(side == "no")])
        .await?;
    let plans = plans(candidate, quantity, &token, &markets.window)?;
    let (net, share_fee, projected) = conservative(candidate, &plans, quantity)?;
    if net <= d::one() {
        return Ok(None);
    }
    let mut reserved = [d::zero(), d::zero()];
    for i in 0..2 {
        reserved[i] = d::add(
            &d::mul(
                &d::amount(&plans[i]["cash_budget"])?,
                &d::add(&d::one(), &d::amount(&candidate["configs"][i]["rate"])?),
            ),
            &d::parse("0.01")?,
        );
    }
    clients.authenticate().await?;
    let balances = clients.balances().await?;
    ensure!(
        (0..2).all(|i| balances[i] >= 15 && balances[i] >= reserved[i]),
        "Insufficient available collateral"
    );
    clients.approvals(markets, &reserved).await?;
    if btc::entry_reason(
        &markets.window,
        &candidate["result"],
        timestamps,
        crate::now_ms(),
    )?
    .is_some()
        || !feed.connected().await
    {
        return Ok(None);
    }
    let poly = clients.poly.as_ref().context("Polymarket SDK missing")?;
    let (poly_signed, poly_id) = poly
        .prepare(
            &plans[0],
            &token,
            false,
            None,
            crate::now_ms(),
            signing::salt(false),
        )
        .await?;
    let (predict_body, predict_id) = signing::predict_payload(
        &plans[1],
        &markets.predict_info,
        &clients.predict_wallet,
        clients.predict_account,
        crate::now_ms() / 1000,
        signing::salt(true),
    )?;
    if btc::entry_reason(
        &markets.window,
        &candidate["result"],
        timestamps,
        crate::now_ms(),
    )?
    .is_some()
        || !feed.connected().await
    {
        return Ok(None);
    }
    let attempt = uuid::Uuid::new_v4().simple().to_string();
    journal.write("INTENT",&attempt,json!({"window_start":start,"window_end":markets.window["end"],"sides":candidate["sides"],"requests":plans,"reserved":reserved.iter().map(d::json).collect::<Vec<_>>(),"execution":"CONCURRENT_CASH_MARKET","entry_min_net_total":"1","max_net_share_gap":"0.5","run_attempt":attempt_limits.0,"run_max_trades":attempt_limits.1,"estimated_predict_share_fee":d::json(&share_fee),"estimated_received_shares":projected.iter().map(d::json).collect::<Vec<_>>(),"conservative_entry_net_total":d::json(&net)}))?;
    journal.windows.insert(start);
    let poly_cash = signing::from_raw(poly_signed.order().makerAmount, 6)?;
    let poly_size = signing::from_raw(poly_signed.order().takerAmount, 6)?;
    let predict_cash = signing::from_raw(
        U256::from_str(
            predict_body["data"]["order"]["makerAmount"]
                .as_str()
                .context("Missing cash")?,
        )?,
        18,
    )?;
    let predict_size = signing::from_raw(
        U256::from_str(
            predict_body["data"]["order"]["takerAmount"]
                .as_str()
                .context("Missing shares")?,
        )?,
        18,
    )?;
    for (i, id, cash, size) in [
        (0, &poly_id, &poly_cash, &poly_size),
        (1, &predict_id, &predict_cash, &predict_size),
    ] {
        ensure!(
            cash <= &d::amount(&plans[i]["cash_budget"])?,
            "Signed cash exceeds planned budget"
        );
        let mut fields = json!({"leg":i,"order_id":id,"cash_budget":d::json(cash),"target_shares":d::json(quantity)});
        fields[if i == 0 {
            "signed_minimum_shares"
        } else {
            "signed_target_shares"
        }] = d::json(size);
        journal.write("CASH_SUBMIT_INTENT", &attempt, fields)?;
    }
    let mut orders = vec![
        safe_order(&poly_id, 0, &plans[0], &poly_size, false),
        safe_order(&predict_id, 1, &plans[1], &predict_size, false),
    ];
    feed.watch_orders(&orders).await?;
    let (poly_result, predict_result) = tokio::join!(
        poly.submit(poly_signed),
        clients.private(1, Method::POST, "/orders", &[], Some(predict_body))
    );
    for (i, result) in [poly_result, predict_result].into_iter().enumerate() {
        match result {
            Ok(raw) => {
                let acknowledged = if i == 0 {
                    raw["orderID"].as_str()
                } else {
                    raw["data"]["orderHash"].as_str()
                };
                if let Some(id) = acknowledged.filter(|s| !s.is_empty()) {
                    orders[i]["id"] = json!(id);
                }
                journal.write("ORDER",&attempt,json!({"leg":i,"order":orders[i],"cash_budget":plans[i]["cash_budget"],"target_shares":d::json(quantity)}))?;
            }
            Err(error) => {
                if error.to_string() == "VENUE_REJECTED" {
                    orders[i] = json!("REJECTED");
                    journal.write(
                        "ORDER",
                        &attempt,
                        json!({"leg":i,"rejected":"OrderRejected","venue_reason":"VENUE_REJECTED"}),
                    )?;
                } else {
                    journal.write(
                        "ORDER",
                        &attempt,
                        json!({"leg":i,"unknown":"SubmitUnknown"}),
                    )?;
                }
            }
        }
    }
    feed.watch_orders(&orders).await?;
    let record = json!({"type":"AWAIT_CONFIRMATION","attempt":attempt,"window":markets.window,"sides":candidate["sides"],"orders":orders,"plans":plans,"run_attempt":attempt_limits.0,"run_max_trades":attempt_limits.1,"new_entries":false});
    let deadline = Instant::now()
        + Duration::from_secs(45.min(
            (markets.window["end"].as_i64().unwrap_or(0) - crate::now_ms() / 1000 - 15).max(0)
                as u64,
        ));
    loop {
        let inventories = query_inventories(clients, markets, &record, feed).await;
        if let Some(inventories) = inventories {
            return after_fills(clients, journal, markets, &record, inventories, feed, false)
                .await
                .map(Some);
        }
        if Instant::now() >= deadline {
            let fields = record
                .as_object()
                .unwrap()
                .iter()
                .filter(|(key, _)| !matches!(key.as_str(), "type" | "attempt"))
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            return Ok(Some(journal.write(
                "AWAIT_CONFIRMATION",
                &attempt,
                Value::Object(fields),
            )?));
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

pub async fn query_inventories(
    clients: &Clients,
    markets: &Markets,
    record: &Value,
    feed: &crate::feed::Feeds,
) -> Option<[Value; 2]> {
    if feed.conflicts().await {
        return None;
    }
    let ws = feed.receipts().await;
    let (a, b) = tokio::join!(
        confirmed(clients, 0, &record["orders"][0], markets, ws[0].clone()),
        confirmed(clients, 1, &record["orders"][1], markets, ws[1].clone())
    );
    match (a, b) {
        (Ok(Some(a)), Ok(Some(b))) => Some([a, b]),
        _ => None,
    }
}

async fn after_fills(
    clients: &Clients,
    journal: &mut Journal,
    markets: &Markets,
    record: &Value,
    inventories: [Value; 2],
    feed: &crate::feed::Feeds,
    delayed: bool,
) -> Result<Value> {
    let attempt = record["attempt"].as_str().context("Missing attempt")?;
    let mut remaining = [
        d::amount(&inventories[0]["net_shares"])?,
        d::amount(&inventories[1]["net_shares"])?,
    ];
    journal.write("FILLS",attempt,json!({"confirmed_shares":inventories.iter().map(|r|r["gross_shares"].clone()).collect::<Vec<_>>(),"confirmed_net_shares":remaining.iter().map(d::json).collect::<Vec<_>>(),"cash_values":inventories.iter().map(|r|r["cash_value"].clone()).collect::<Vec<_>>(),"share_fees":inventories.iter().map(|r|r["share_fees"].clone()).collect::<Vec<_>>(),"new_entries":false}))?;
    for (i, inventory) in inventories.iter().enumerate() {
        if d::amount(&inventory["cash_value"])?
            > d::add(
                &d::amount(&record["plans"][i]["cash_budget"])?,
                &d::parse("0.000001")?,
            )
        {
            journal.write("BUDGET_OVERRUN",attempt,json!({"leg":i,"actual":inventory["cash_value"],"budget":record["plans"][i]["cash_budget"]}))?;
        }
    }
    if remaining.iter().all(|s| *s == d::zero()) {
        return journal.write("DONE", attempt, json!({"state":"NO_FILL"}));
    }
    let gap = (&remaining[0] - &remaining[1]).abs();
    let mut exits = Vec::new();
    let mut state = "PAIRED";
    if delayed {
        state = "CONFIRMED_AFTER_DELAY_REVIEW_REQUIRED";
    } else if gap > d::parse("0.5")? {
        state = "UNBALANCED_ENTRY";
        let index = usize::from(remaining[1] > remaining[0]);
        let side = record["sides"][index].as_str().context("Missing side")?;
        let amount = gap.min(clients.available_shares(index, markets, side).await?);
        let amount = if index == 0 {
            amount.with_scale_round(2, RoundingMode::Down)
        } else {
            signing::from_raw(signing::significant(signing::raw(&amount, 18)?, 5), 18)?
        };
        let bids = feed.bids(index, side).await;
        let mut left = amount.clone();
        let mut cap = None;
        for row in bids.as_array().context("Invalid exit book")? {
            let price = d::amount(&row[0])?;
            let size = d::amount(&row[1])?;
            if price <= d::zero() || price >= d::one() || size <= d::zero() {
                continue;
            }
            left = d::sub(&left, &size.min(left.clone()));
            if left == d::zero() {
                cap = Some(price);
                break;
            }
        }
        if amount > d::zero()
            && let Some(cap) = cap
        {
            let mut plan = record["plans"][index].clone();
            plan["target_shares"] = d::json(&amount);
            journal.write("UNWIND_INTENT",attempt,json!({"leg":index,"request":{"market_id":plan["market_id"],"amount":d::json(&amount),"outcome":side,"price":d::json(&cap),"reduce_only":true}}))?;
            let (order, result) = if index == 0 {
                let token = clients
                    .data
                    .token_info(&markets.tokens[usize::from(side == "no")])
                    .await?;
                let poly = clients.poly.as_ref().context("SDK missing")?;
                let (signed, id) = poly
                    .prepare(
                        &plan,
                        &token,
                        true,
                        Some(cap),
                        crate::now_ms(),
                        signing::salt(false),
                    )
                    .await?;
                let order = safe_order(&id, index, &plan, &amount, true);
                journal.write(
                    "UNWIND_SUBMIT_INTENT",
                    attempt,
                    json!({"leg":index,"order":order}),
                )?;
                feed.watch_orders(std::slice::from_ref(&order)).await?;
                (order, poly.submit(signed).await)
            } else {
                let price = signing::significant(signing::raw(&cap, 18)?, 3);
                let shares = signing::significant(signing::raw(&amount, 18)?, 5);
                let cash = price * shares / U256::from(10).pow(U256::from(18));
                let (mut body, id) = signing::predict_order(
                    &clients.predict_wallet,
                    clients.predict_account,
                    &markets.predict_info,
                    side,
                    shares,
                    cash,
                    1,
                    crate::now_ms() / 1000,
                    signing::salt(true),
                )?;
                body["data"]["pricePerShare"] = json!(price.to_string());
                let order = safe_order(&id, index, &plan, &amount, true);
                journal.write(
                    "UNWIND_SUBMIT_INTENT",
                    attempt,
                    json!({"leg":index,"order":order}),
                )?;
                feed.watch_orders(std::slice::from_ref(&order)).await?;
                (
                    order,
                    clients
                        .private(index, Method::POST, "/orders", &[], Some(body))
                        .await,
                )
            };
            journal.write(
                "UNWIND_ORDER",
                attempt,
                json!({"leg":index,"order":order,"submission_acknowledged":result.is_ok()}),
            )?;
            let deadline = Instant::now() + Duration::from_secs(30);
            let mut sold = None;
            if result
                .as_ref()
                .is_err_and(|error| error.to_string() == "VENUE_REJECTED")
            {
                sold = Some(inventory(&HashMap::new(), true)?);
            }
            while sold.is_none() && Instant::now() < deadline {
                if let Ok(Some(inventory)) = confirmed(
                    clients,
                    index,
                    &order,
                    markets,
                    feed.receipts().await[index].clone(),
                )
                .await
                {
                    sold = Some(inventory);
                    break;
                }
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
            if let Some(inventory) = sold {
                let sold = d::amount(&inventory["gross_shares"])?;
                ensure!(sold <= remaining[index], "Exit exceeds inventory");
                remaining[index] = d::sub(&remaining[index], &sold);
                exits.push(inventory);
                journal.write("UNWIND_RESULT",attempt,json!({"leg":index,"confirmed_sold":d::json(&sold),"remaining_net_shares":remaining.iter().map(d::json).collect::<Vec<_>>()}))?;
            } else {
                journal.write(
                    "HALT",
                    attempt,
                    json!({"reason":"UNWIND_CONFIRMATION_UNKNOWN","leg":index}),
                )?;
                anyhow::bail!("Exit outcome unknown; reconcile journal before restarting");
            }
        } else {
            journal.write("ENTRY_STOPPED",attempt,json!({"reason":if amount<=d::zero(){"NO_SELLABLE_INVENTORY"}else{"UNWIND_DEPTH_UNAVAILABLE"},"leg":index}))?;
        }
    }
    let stop_after = delayed
        || state != "PAIRED"
        || record["run_attempt"].as_u64().unwrap_or(1)
            >= record["run_max_trades"].as_u64().unwrap_or(1);
    journal.write("AWAIT_SETTLEMENT",attempt,json!({"state":state,"window":markets.window,"sides":record["sides"],"remaining_net_shares":remaining.iter().map(d::json).collect::<Vec<_>>(),"stop_after":stop_after,"entry_cash_values":inventories.iter().map(|r|r["cash_value"].clone()).collect::<Vec<_>>(),"entry_cash_fees":inventories.iter().map(|r|r["cash_fee"].clone()).collect::<Vec<_>>(),"exit_cash_values":exits.iter().map(|r|r["cash_value"].clone()).collect::<Vec<_>>(),"exit_cash_fees":exits.iter().map(|r|r["cash_fee"].clone()).collect::<Vec<_>>()}))
}

pub async fn resume_confirmation(
    clients: &mut Clients,
    journal: &mut Journal,
    record: &Value,
    feed: &crate::feed::Feeds,
) -> Result<Value> {
    let markets = clients.data.load(record["window"].clone()).await?;
    loop {
        if clients.authenticate().await.is_err() {
            log::info!("LIVE_CONFIRMATION_AUTH_RETRY new_entries=false");
            tokio::time::sleep(Duration::from_secs(5)).await;
            continue;
        }
        if let Some(inventories) = query_inventories(clients, &markets, record, feed).await {
            return after_fills(clients, journal, &markets, record, inventories, feed, true).await;
        }
        log::info!("LIVE_WAIT_CONFIRMATION new_entries=false");
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}

pub async fn wait_settlement(
    clients: &Clients,
    journal: &mut Journal,
    record: &Value,
) -> Result<()> {
    let attempt = record["attempt"].as_str().context("Missing attempt")?;
    loop {
        if crate::now_ms() / 1000
            >= record["window"]["end"]
                .as_i64()
                .context("Invalid window end")?
            && let Ok(Some(factors)) = clients.data.settlement(record).await
        {
            let payouts = [
                d::mul(&d::amount(&record["remaining_net_shares"][0])?, &factors[0]),
                d::mul(&d::amount(&record["remaining_net_shares"][1])?, &factors[1]),
            ];
            let total = d::add(&payouts[0], &payouts[1]);
            let sum = |values: &Value| -> Result<Option<Decimal>> {
                let mut total = d::zero();
                for v in values.as_array().context("Invalid cash-flow rows")? {
                    if v.is_null() {
                        return Ok(None);
                    }
                    total = d::add(&total, &d::amount(v)?);
                }
                Ok(Some(total))
            };
            let cost = sum(&record["entry_cash_values"])?;
            let proceeds = sum(&record["exit_cash_values"])?;
            let fees = match (
                sum(&record["entry_cash_fees"])?,
                sum(&record["exit_cash_fees"])?,
            ) {
                (Some(a), Some(b)) => Some(d::add(&a, &b)),
                _ => None,
            };
            let gross = match (&cost, &proceeds) {
                (Some(cost), Some(proceeds)) => Some(d::sub(&d::add(&total, proceeds), cost)),
                _ => None,
            };
            let net = match (&gross, &fees) {
                (Some(gross), Some(fees)) => Some(d::sub(gross, fees)),
                _ => None,
            };
            journal.write("SETTLEMENT",attempt,json!({"payout_factors":factors.iter().map(d::json).collect::<Vec<_>>(),"payouts":payouts.iter().map(d::json).collect::<Vec<_>>(),"total_payout":d::json(&total),"entry_cash_cost":cost.as_ref().map(d::json),"exit_cash_proceeds":proceeds.as_ref().map(d::json),"cash_fees":fees.as_ref().map(d::json),"gross_pnl":gross.as_ref().map(d::json),"net_pnl":net.as_ref().map(d::json),"pnl_state":if net.is_some(){"VERIFIED_FEES"}else{"CASH_FLOW_OR_FEES_UNKNOWN"},"payout_state":"EVENT_RESOLVED_REDEMPTION_NOT_VERIFIED"}))?;
            journal.write(
                "DONE",
                attempt,
                json!({"state":"SETTLED","stop_after":record["stop_after"]}),
            )?;
            return Ok(());
        }
        log::info!("LIVE_WAIT_SETTLEMENT new_entries=false");
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}
