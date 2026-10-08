//! Offline regressions for live sizing, receipts and settlement behavior.
use crate::{
    adapters::{self, Receipt},
    btc, decimal as d, pair, signing, trading,
};
use serde_json::{Value, json};
use std::collections::HashMap;

fn configs() -> Value {
    json!([{"model":"quadratic_theta","rate":"0.07","places":"0.00001"},{"model":"min_price","rate":"0.02","places":"0.000001"}])
}
fn calculate(a: Value, b: Value, quantity: &str, configs: Value) -> Value {
    pair::calculate(&json!({"asks_a":a,"asks_b":b,"quantity":quantity,"configs":configs})).unwrap()
}
fn receipt(state: &str, amount: &str) -> Receipt {
    Receipt {
        id: "trade".into(),
        order_id: "order".into(),
        amount: d::parse(amount).unwrap(),
        price: d::parse("0.32").unwrap(),
        side: "BUY".into(),
        settlement: state.into(),
        fee: None,
        fee_currency: None,
    }
}

#[test]
fn unknown_fee_and_partial_depth_do_not_become_entry_signals() {
    let result = calculate(
        json!([["0.4", "10"]]),
        json!([["0.45", "10"]]),
        "10",
        json!([null, null]),
    );
    assert_eq!(result["state"], "FEES_UNKNOWN");
    assert!(result["net"].is_null());
    let result = calculate(
        json!([["0.4", "0.61"]]),
        json!([["0.45", "10"]]),
        "10",
        json!([null, null]),
    );
    assert_eq!(result["state"], "INSUFFICIENT_DEPTH");
    assert!(result["gross"].is_null());
    assert_eq!(
        d::amount(&result["legs"][0]["filled"]).unwrap(),
        d::parse("0.61").unwrap()
    );
}
#[test]
fn positive_gross_with_fees_can_be_negative() {
    let result = calculate(
        json!([["0.49", "10"]]),
        json!([["0.49", "10"]]),
        "10",
        configs(),
    );
    assert_eq!(result["state"], "NO_NET_EDGE");
    assert!(d::amount(&result["gross"]).unwrap() > d::zero());
}
#[test]
fn cash_plan_preserves_ten_shares_and_slippage_cash() {
    let candidate = json!({"sides":["yes","no"],"result":{"legs":[{"cost":"3.20"},{"cost":"3.20"}]},"asks":[[],[["0.32","100"]]],"ticks":["0.01","0.001"]});
    let plans = trading::plans(
        &candidate,
        &d::parse("10").unwrap(),
        &json!({"tick":"0.01"}),
        &json!({"polymarket":"1","predictfun":"2"}),
    )
    .unwrap();
    assert_eq!(
        d::amount(&plans[1]["cash_budget"]).unwrap(),
        d::parse("3.232").unwrap()
    );
    assert_eq!(
        d::amount(&plans[1]["target_shares"]).unwrap(),
        d::parse("10").unwrap()
    );
    assert_eq!(
        d::amount(&plans[1]["expected_price"]).unwrap(),
        d::parse("0.32").unwrap()
    );
}
#[test]
fn multi_level_plan_reserves_worst_tier_and_not_average() {
    let candidate = json!({"sides":["yes","no"],"result":{"legs":[{"cost":"3.20"},{"cost":"3.60"}]},"asks":[[],[["0.30","4"],["0.40","6"]]],"ticks":["0.01","0.001"]});
    let plans = trading::plans(
        &candidate,
        &d::parse("10").unwrap(),
        &json!({"tick":"0.01"}),
        &json!({"polymarket":"1","predictfun":"2"}),
    )
    .unwrap();
    assert_eq!(
        d::amount(&plans[1]["cash_budget"]).unwrap(),
        d::parse("4.04").unwrap()
    );
    assert_eq!(
        d::amount(&plans[1]["expected_price"]).unwrap(),
        d::parse("0.36").unwrap()
    );
}
#[test]
fn minimum_depth_tick_and_live_size_are_required() {
    let mut candidate = json!({"sides":["yes","no"],"result":{"legs":[{"cost":"3.20"},{"cost":"3.20"}]},"asks":[[],[["0.32","1"]]],"ticks":["0.01","0.001"]});
    let window = json!({"polymarket":"1","predictfun":"2"});
    let token = json!({"tick":"0.01"});
    assert!(trading::plans(&candidate, &d::parse("10").unwrap(), &token, &window).is_err());
    candidate["asks"][1] = json!([["0.32", "100"]]);
    candidate["ticks"][1] = Value::Null;
    assert!(trading::plans(&candidate, &d::parse("10").unwrap(), &token, &window).is_err());
    assert!(trading::plans(&candidate, &d::parse("11").unwrap(), &token, &window).is_err());
}
#[test]
fn predict_fee_reduces_inventory_without_increasing_target() {
    let mut fill = receipt("CONFIRMED", "10");
    fill.fee = Some(d::parse("0.2").unwrap());
    fill.fee_currency = Some("shares".into());
    let fills = HashMap::from([("trade".into(), fill)]);
    let inventory = trading::inventory(&fills, false).unwrap();
    assert_eq!(
        d::amount(&inventory["net_shares"]).unwrap(),
        d::parse("9.8").unwrap()
    );
    let inventory = trading::inventory(&fills, true).unwrap();
    assert_eq!(
        d::amount(&inventory["net_shares"]).unwrap(),
        d::parse("10").unwrap()
    );
}
#[test]
fn unknown_or_excessive_share_fees_block_inventory() {
    let mut fill = receipt("CONFIRMED", "10");
    fill.fee_currency = Some("shares".into());
    assert!(trading::inventory(&HashMap::from([("trade".into(), fill.clone())]), false).is_err());
    fill.fee = Some(d::parse("11").unwrap());
    assert!(trading::inventory(&HashMap::from([("trade".into(), fill)]), false).is_err());
}
#[test]
fn receipt_terminal_states_cannot_regress_or_conflict() {
    let confirmed = receipt("CONFIRMED", "10");
    let matched = receipt("MATCHED", "10");
    let merged = adapters::merge_receipts(vec![confirmed.clone()], vec![matched], false).unwrap();
    assert_eq!(merged["trade"].settlement, "CONFIRMED");
    assert!(
        adapters::merge_receipts(
            vec![confirmed.clone()],
            vec![receipt("FAILED", "10")],
            false
        )
        .is_err()
    );
    assert!(
        adapters::merge_receipts(vec![confirmed], vec![receipt("CONFIRMED", "9")], false).is_err()
    );
}
#[test]
fn predict_receipt_suffix_is_deduplicated_and_fees_can_be_enriched() {
    let original = receipt("CONFIRMED", "10");
    let mut rest = original.clone();
    rest.id = "trade:leg".into();
    rest.fee = Some(d::parse("0.2").unwrap());
    rest.fee_currency = Some("shares".into());
    let merged = adapters::merge_receipts(vec![original], vec![rest], true).unwrap();
    assert_eq!(merged.len(), 1);
    assert_eq!(merged["trade:order"].fee, Some(d::parse("0.2").unwrap()));
}
#[test]
fn entry_cutoff_staleness_skew_and_strict_profit_threshold() {
    let result = calculate(
        json!([["0.2", "10"]]),
        json!([["0.32", "10"]]),
        "10",
        configs(),
    );
    let window = json!({"end":900});
    assert!(
        btc::entry_reason(&window, &result, [601000, 601000], 601000)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        btc::entry_reason(&window, &result, [840000, 840000], 840000).unwrap(),
        Some("SIZE_OR_TIME_NOT_ELIGIBLE")
    );
    assert_eq!(
        btc::entry_reason(&window, &result, [580000, 601000], 601000).unwrap(),
        Some("QUOTE_TIME_UNKNOWN_OR_STALE")
    );
    assert_eq!(
        btc::entry_reason(&window, &result, [590000, 601000], 601000).unwrap(),
        Some("QUOTE_TIME_SKEW")
    );
    let mut exact = result;
    exact["net"] = json!("1.02");
    assert_eq!(
        btc::entry_reason(&window, &exact, [601000, 601000], 601000).unwrap(),
        Some("NET_TOTAL_NOT_ABOVE_1_AFTER_BUFFER")
    );
}
#[test]
fn settlement_requires_both_venues_and_accepts_both_legs_losing() {
    let poly = json!({"outcomes":"[\"Up\",\"Down\"]","umaResolutionStatus":"resolved","outcomePrices":"[\"0\",\"1\"]"});
    let mut predict = json!({"status":"OPEN","outcomes":[{"indexSet":1,"name":"Up","status":"WON"},{"indexSet":2,"name":"Down","status":"LOST"}]});
    let sides = vec![json!("yes"), json!("no")];
    assert!(
        btc::settlement_factors(&poly, &predict, &sides, &Value::Null)
            .unwrap()
            .is_none()
    );
    predict["status"] = json!("RESOLVED");
    let factors = btc::settlement_factors(&poly, &predict, &sides, &Value::Null)
        .unwrap()
        .unwrap();
    assert_eq!(factors, [d::zero(), d::zero()]);
    predict["outcomes"][0]["status"] = json!("VOID");
    assert!(
        btc::settlement_factors(&poly, &predict, &sides, &Value::Null)
            .unwrap()
            .is_none()
    );
}
#[test]
fn raw_uint256_and_significant_shares_remain_exact() {
    let max = U256::MAX;
    let value = signing::from_raw(max, 18).unwrap();
    assert_eq!(signing::raw(&value, 18).unwrap(), max);
    assert_eq!(
        signing::significant(
            signing::raw(&d::parse("10.000123").unwrap(), 18).unwrap(),
            5
        ),
        signing::raw(&d::parse("10").unwrap(), 18).unwrap()
    );
}
use alloy_primitives::U256;

#[tokio::test]
async fn order_transport_failure_does_not_retry_the_post() {
    use std::{
        io::Read,
        net::TcpListener,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
        time::Duration,
    };
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    listener.set_nonblocking(true).unwrap();
    let count = Arc::new(AtomicUsize::new(0));
    let observed = count.clone();
    let server = std::thread::spawn(move || {
        let until = std::time::Instant::now() + Duration::from_millis(400);
        while std::time::Instant::now() < until {
            if let Ok((mut stream, _)) = listener.accept() {
                observed.fetch_add(1, Ordering::SeqCst);
                stream
                    .set_read_timeout(Some(Duration::from_millis(100)))
                    .unwrap();
                let mut buf = [0; 2048];
                let _ = stream.read(&mut buf);
            } else {
                std::thread::sleep(Duration::from_millis(5));
            }
        }
    });
    let result = adapters::request(
        &adapters::http().unwrap(),
        reqwest::Method::POST,
        &format!("http://{address}/orders"),
        &[],
        reqwest::header::HeaderMap::new(),
        Some("{}".into()),
    )
    .await;
    assert!(result.is_err());
    server.join().unwrap();
    assert_eq!(count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn official_polymarket_sdk_signs_cash_buy_for_deposit_wallet_offline() {
    let key = format!("0x{}", "1".repeat(64));
    let maker = "0x2222222222222222222222222222222222222222"
        .parse()
        .unwrap();
    let creds = polymarket_client_sdk_v2::auth::Credentials::new(
        uuid::Uuid::nil(),
        "dGVzdA==".into(),
        "offline".into(),
    );
    let poly = crate::polymarket::Polymarket::connect(&key, maker, 3, Some(creds))
        .await
        .unwrap();
    let plan = json!({"cash_budget":"1.70","target_shares":"10"});
    let token = json!({"token_id":"123","tick":"0.01","neg_risk":false});
    let (signed, hash) = poly
        .prepare(&plan, &token, false, None, 1, 1)
        .await
        .unwrap();
    assert_eq!(signed.order().makerAmount, U256::from(1700000));
    assert_eq!(signed.order().takerAmount, U256::from(1717100));
    assert_eq!(signed.order().side, 0);
    assert_eq!(signed.order().signer, maker);
    assert!(hash.starts_with("0x"));
    let payload = serde_json::to_value(&signed).unwrap();
    assert!(payload["order"]["signature"].as_str().unwrap().len() > 132);
}
