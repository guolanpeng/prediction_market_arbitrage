//! Standalone REST adapters. No Nautilus engine, Python bridge, or write retries.
use crate::{
    btc,
    decimal::{self as d, Decimal},
    signing::{self, Wallet},
};
use alloy_primitives::{Address, U256, hex, keccak256};
use alloy_sol_types::SolValue;
use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use reqwest::{Client, Method, header::HeaderMap};
use serde_json::{Value, json};
use std::{collections::HashMap, env, str::FromStr, time::Duration};

pub const GAMMA: &str = "https://gamma-api.polymarket.com";
pub const CLOB: &str = "https://clob.polymarket.com";
pub const PREDICT: &str = "https://api.predict.fun/v1";

pub fn http() -> Result<Client> {
    init_tls();
    Ok(Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(30))
        .user_agent("prediction-btc-rust/1")
        .build()?)
}

pub fn init_tls() {
    // reqwest versions select different providers. WebSocket's default builder
    // needs one unambiguous process-wide provider when both are compiled in.
    static TLS: std::sync::Once = std::sync::Once::new();
    TLS.call_once(|| {
        let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    });
}

pub async fn request(
    client: &Client,
    method: Method,
    url: &str,
    params: &[(String, String)],
    headers: HeaderMap,
    body: Option<String>,
) -> Result<Value> {
    let order_post = method == Method::POST && url == format!("{PREDICT}/orders");
    let mut request = client.request(method, url).query(params).headers(headers);
    if let Some(body) = body {
        request = request
            .header("Content-Type", "application/json")
            .body(body);
    }
    let response = request
        .send()
        .await
        .map_err(|_| anyhow::anyhow!("TRANSPORT_UNKNOWN"))?;
    let status = response.status();
    if !status.is_success() {
        if order_post && matches!(status.as_u16(), 400 | 401 | 403 | 404 | 409 | 422 | 429) {
            bail!("VENUE_REJECTED");
        }
        bail!("HTTP_{}", status.as_u16());
    }
    let text = response
        .text()
        .await
        .map_err(|_| anyhow::anyhow!("RESPONSE_UNKNOWN"))?;
    let value: Value = serde_json::from_str(&text).map_err(|_| anyhow::anyhow!("INVALID_JSON"))?;
    if value.get("success") == Some(&json!(false)) {
        bail!("VENUE_REJECTED");
    }
    Ok(value)
}

fn header(headers: &mut HeaderMap, name: &'static str, value: &str) -> Result<()> {
    headers.insert(
        name,
        value
            .parse()
            .map_err(|_| anyhow::anyhow!("Invalid authentication header"))?,
    );
    Ok(())
}
pub struct MarketData {
    pub http: Client,
    pub api_key: String,
}
pub struct Markets {
    pub window: Value,
    pub poly: Value,
    pub predict: Value,
    pub tokens: [String; 2],
    pub configs: [Value; 2],
    pub predict_info: Value,
}

impl MarketData {
    pub async fn predict(&self, path: &str, params: &[(String, String)]) -> Result<Value> {
        let mut headers = HeaderMap::new();
        header(&mut headers, "x-api-key", &self.api_key)?;
        request(
            &self.http,
            Method::GET,
            &format!("{PREDICT}{path}"),
            params,
            headers,
            None,
        )
        .await
    }
    pub async fn poly(&self, base: &str, path: &str, params: &[(String, String)]) -> Result<Value> {
        request(
            &self.http,
            Method::GET,
            &format!("{base}{path}"),
            params,
            HeaderMap::new(),
            None,
        )
        .await
    }
    pub async fn discover(&self, start: i64) -> Result<Markets> {
        let slug = format!("btc-updown-5m-{start}");
        let params = [("slug".into(), slug.clone())];
        let path = format!("/categories/{slug}");
        let (poly, predict) = tokio::try_join!(
            self.poly(GAMMA, "/markets", &params),
            self.predict(&path, &[])
        )?;
        let window = btc::validate_window(&poly, &predict["data"], start)?;
        self.load(window).await
    }
    pub async fn load(&self, window: Value) -> Result<Markets> {
        let poly_id = window["polymarket"]
            .as_str()
            .context("Missing Polymarket ID")?;
        let predict_id = window["predictfun"]
            .as_str()
            .context("Missing Predict ID")?;
        let poly_path = format!("/markets/{poly_id}");
        let predict_path = format!("/markets/{predict_id}");
        let (poly, predict) = tokio::try_join!(
            self.poly(GAMMA, &poly_path, &[]),
            self.predict(&predict_path, &[])
        )?;
        let predict = predict["data"].clone();
        ensure!(
            btc::array(&poly["outcomes"])? == vec![json!("Up"), json!("Down")],
            "Polymarket orientation changed"
        );
        let tokens = btc::array(&poly["clobTokenIds"])?;
        ensure!(tokens.len() == 2, "Missing Polymarket tokens");
        let tokens = [
            tokens[0].as_str().context("Invalid token")?.to_string(),
            tokens[1].as_str().context("Invalid token")?.to_string(),
        ];
        let outcomes = btc::array(&predict["outcomes"])?;
        let token = |index: i64| -> Result<String> {
            let row = outcomes
                .iter()
                .find(|r| r["indexSet"] == index)
                .context("Missing Predict token")?;
            Ok(row["onChainId"]
                .as_str()
                .context("Missing on-chain token ID")?
                .into())
        };
        ensure!(
            outcomes.len() == 2
                && outcomes
                    .iter()
                    .any(|r| r["indexSet"] == 1 && r["name"] == "Up")
                && outcomes
                    .iter()
                    .any(|r| r["indexSet"] == 2 && r["name"] == "Down"),
            "Predict orientation changed"
        );
        let mut config = Value::Null;
        if poly["feesEnabled"] == false {
            config = json!({"model":"quadratic_theta","rate":"0","places":"0.00001"});
        } else if !poly["feeSchedule"]["rate"].is_null() {
            let rate = d::amount(&poly["feeSchedule"]["rate"])?;
            let exponent = if poly["feeSchedule"]["exponent"].is_null() {
                d::one()
            } else {
                d::amount(&poly["feeSchedule"]["exponent"])?
            };
            if rate >= d::zero() && exponent == d::one() {
                config =
                    json!({"model":"quadratic_theta","rate":d::json(&rate),"places":"0.00001"});
            }
        }
        let fee = predict["feeRateBps"]
            .as_u64()
            .context("Unknown Predict fee rate")?;
        let configs = [
            config,
            json!({"model":"min_price","rate":d::json(&d::div(&Decimal::from(fee),&Decimal::from(10000))?),"places":"0.000001"}),
        ];
        ensure!(
            predict["isNegRisk"].is_boolean() && predict["isYieldBearing"].is_boolean(),
            "Unknown Predict contract variant"
        );
        let info = json!({"yes_token":token(1)?,"no_token":token(2)?,"neg_risk":predict["isNegRisk"],"yield_bearing":predict["isYieldBearing"],"fee_rate_bps":fee});
        Ok(Markets {
            window,
            poly,
            predict,
            tokens,
            configs,
            predict_info: info,
        })
    }
    pub async fn token_info(&self, token: &str) -> Result<Value> {
        let book = self
            .poly(CLOB, "/book", &[("token_id".into(), token.into())])
            .await?;
        ensure!(book["neg_risk"].is_boolean(), "Unknown Polymarket exchange");
        Ok(
            json!({"token_id":token,"tick":book["tick_size"],"min_size":book["min_order_size"],"neg_risk":book["neg_risk"],"condition_id":book["market"]}),
        )
    }
    pub async fn settlement(&self, record: &Value) -> Result<Option<[Decimal; 2]>> {
        let window = &record["window"];
        let sides = record["sides"].as_array().context("Invalid sides")?;
        let poly_path = format!(
            "/markets/{}",
            window["polymarket"].as_str().context("Missing ID")?
        );
        let predict_path = format!(
            "/markets/{}",
            window["predictfun"].as_str().context("Missing ID")?
        );
        let params = [("_".into(), crate::now_ms().to_string())];
        let (poly, predict) = tokio::try_join!(
            self.poly(GAMMA, &poly_path, &params),
            self.predict(&predict_path, &[])
        )?;
        let clob = if poly["umaResolutionStatus"] != "resolved"
            && predict["data"]["status"] == "RESOLVED"
        {
            self.poly(
                CLOB,
                &format!(
                    "/markets/{}",
                    poly["conditionId"].as_str().context("Missing condition")?
                ),
                &[("_".into(), crate::now_ms().to_string())],
            )
            .await?
        } else {
            Value::Null
        };
        btc::settlement_factors(&poly, &predict["data"], sides, &clob)
    }
}

pub struct Clients {
    pub data: MarketData,
    pub poly_wallet: Wallet,
    pub predict_wallet: Wallet,
    pub poly_maker: Address,
    pub predict_account: Option<Address>,
    pub signature_type: u8,
    pub poly_api: Value,
    pub poly: Option<crate::polymarket::Polymarket>,
    pub jwt: String,
    jwt_expires: i64,
    pub poly_rpc: String,
    pub predict_rpc: String,
}

impl Clients {
    pub fn from_env() -> Result<Self> {
        let required = |name: &str| -> Result<String> {
            let value =
                env::var(name).map_err(|_| anyhow::anyhow!("Missing configuration: {name}"))?;
            ensure!(!value.is_empty(), "Missing configuration: {name}");
            Ok(value)
        };
        ensure!(
            matches!(
                env::var("PREDICT_FUN_TESTNET")
                    .unwrap_or_default()
                    .to_lowercase()
                    .as_str(),
                "" | "false" | "0"
            ),
            "BTC live runner requires mainnet"
        );
        let poly_wallet = Wallet::new(&required("POLYMARKET_PRIVATE_KEY")?)?;
        let predict_wallet = Wallet::new(&required("PREDICT_FUN_PRIVATE_KEY")?)?;
        let signature_type = required("POLYMARKET_SIGNATURE_TYPE")?.parse::<u8>()?;
        ensure!(signature_type <= 3, "Invalid signature type");
        let poly_maker = if signature_type == 0 {
            poly_wallet.address
        } else {
            Address::from_str(&required("POLYMARKET_FUNDER")?)?
        };
        let predict_account = env::var("PREDICT_FUN_ACCOUNT_ADDRESS")
            .ok()
            .filter(|v| !v.is_empty())
            .map(|v| Address::from_str(&v))
            .transpose()?;
        let poly_api = json!({"apiKey":env::var("POLYMARKET_API_KEY").unwrap_or_default(),"secret":env::var("POLYMARKET_API_SECRET").unwrap_or_default(),"passphrase":env::var("POLYMARKET_API_PASSPHRASE").unwrap_or_default()});
        Ok(Self {
            data: MarketData {
                http: http()?,
                api_key: required("PREDICT_FUN_API_KEY")?,
            },
            poly_wallet,
            predict_wallet,
            poly_maker,
            predict_account,
            signature_type,
            poly_api,
            poly: None,
            jwt: String::new(),
            jwt_expires: 0,
            poly_rpc: env::var("POLYMARKET_RPC_URL")
                .unwrap_or_else(|_| "https://polygon-bor-rpc.publicnode.com".into()),
            predict_rpc: env::var("PREDICT_FUN_RPC_URL")
                .unwrap_or_else(|_| "https://bsc-dataseed.bnbchain.org".into()),
        })
    }
    pub fn predict_address(&self) -> Address {
        self.predict_account.unwrap_or(self.predict_wallet.address)
    }
    pub async fn authenticate(&mut self) -> Result<()> {
        if self.poly.is_none() {
            let credentials = if self.poly_api["apiKey"].as_str().unwrap_or("").is_empty() {
                None
            } else {
                Some(polymarket_client_sdk_v2::auth::Credentials::new(
                    uuid::Uuid::parse_str(
                        self.poly_api["apiKey"]
                            .as_str()
                            .context("Invalid API key")?,
                    )?,
                    self.poly_api["secret"]
                        .as_str()
                        .context("Missing API secret")?
                        .to_owned(),
                    self.poly_api["passphrase"]
                        .as_str()
                        .context("Missing API passphrase")?
                        .to_owned(),
                ))
            };
            let poly = crate::polymarket::Polymarket::connect(
                &env::var("POLYMARKET_PRIVATE_KEY")?,
                self.poly_maker,
                self.signature_type,
                credentials,
            )
            .await?;
            self.poly_api = poly.credentials();
            self.poly = Some(poly);
        }
        if self.jwt.is_empty() || self.jwt_expires <= crate::now_ms() / 1000 + 60 {
            let raw = self.data.predict("/auth/message", &[]).await?;
            let message = raw["data"]["message"]
                .as_str()
                .context("Missing login message")?;
            let signature = self.predict_wallet.login(message, self.predict_account)?;
            let mut headers = HeaderMap::new();
            header(&mut headers, "x-api-key", &self.data.api_key)?;
            let answer=request(&self.data.http,Method::POST,&format!("{PREDICT}/auth"),&[],headers,Some(json!({"signer":self.predict_address().to_checksum(None),"message":message,"signature":signature}).to_string())).await?;
            self.jwt = answer["data"]["token"]
                .as_str()
                .context("Missing JWT")?
                .to_string();
            let claims = self.jwt.split('.').nth(1).context("Invalid JWT")?;
            let claims: Value =
                serde_json::from_slice(&URL_SAFE_NO_PAD.decode(claims).context("Invalid JWT")?)?;
            self.jwt_expires = claims["exp"].as_i64().context("JWT expiry missing")?;
        }
        Ok(())
    }
    pub async fn private(
        &self,
        index: usize,
        method: Method,
        path: &str,
        params: &[(String, String)],
        body: Option<Value>,
    ) -> Result<Value> {
        let text = body.map(|v| v.to_string());
        let mut headers = HeaderMap::new();
        if index == 0 {
            let poly = self
                .poly
                .as_ref()
                .context("Polymarket SDK not authenticated")?;
            let param = |name: &str| {
                params
                    .iter()
                    .find(|(key, _)| key == name)
                    .map(|(_, value)| value.as_str())
            };
            return match path {
                "/balance-allowance" => poly.balance(param("token_id")).await,
                "/data/trades" => {
                    poly.trades(
                        param("market").context("Missing condition ID")?,
                        param("next_cursor").map(str::to_owned),
                        param("id"),
                    )
                    .await
                }
                _ if path.starts_with("/data/order/") => poly.order(&path[12..]).await,
                _ => bail!("Unsupported Polymarket SDK operation"),
            };
        }
        header(&mut headers, "x-api-key", &self.data.api_key)?;
        header(
            &mut headers,
            "authorization",
            &format!("Bearer {}", self.jwt),
        )?;
        let base = PREDICT;
        request(
            &self.data.http,
            method,
            &format!("{base}{path}"),
            params,
            headers,
            text,
        )
        .await
    }
    pub async fn rpc(&self, index: usize, to: &str, data: String) -> Result<U256> {
        let url = if index == 0 {
            &self.poly_rpc
        } else {
            &self.predict_rpc
        };
        let raw=request(&self.data.http,Method::POST,url,&[],HeaderMap::new(),Some(json!({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":to,"data":data},"latest"]}).to_string())).await?;
        ensure!(raw["error"].is_null(), "RPC read failed");
        Ok(U256::from_str(
            raw["result"].as_str().context("RPC result missing")?,
        )?)
    }
    pub async fn balances(&self) -> Result<[Decimal; 2]> {
        let params = [
            ("asset_type".into(), "COLLATERAL".into()),
            ("signature_type".into(), self.signature_type.to_string()),
        ];
        let (poly, total) = tokio::try_join!(
            self.private(0, Method::GET, "/balance-allowance", &params, None),
            self.rpc(
                1,
                signing::PREDICT_USDT,
                calldata("balanceOf(address)", self.predict_address().abi_encode())
            )
        )?;
        let reserved = self
            .private(
                1,
                Method::POST,
                "/account/reserved-balances/query",
                &[],
                Some(json!({"assets":[{"type":"USDT"}]})),
            )
            .await?;
        let locked = reserved["data"]
            .as_array()
            .context("Invalid reserved balances")?
            .first()
            .map(|r| shares(&r["amount"]))
            .transpose()?
            .unwrap_or(d::zero());
        Ok([
            d::div(&d::amount(&poly["balance"])?, &Decimal::from(1_000_000))?,
            d::sub(&signing::from_raw(total, 18)?, &locked).max(d::zero()),
        ])
    }
    pub async fn approvals(&self, markets: &Markets, required: &[Decimal; 2]) -> Result<()> {
        let poly_token = self.data.token_info(&markets.tokens[0]).await?;
        let spender = if poly_token["neg_risk"] == true {
            signing::POLY_NEG_EXCHANGE
        } else {
            signing::POLY_EXCHANGE
        };
        for (kind, token, needed) in [
            ("COLLATERAL", None, required[0].clone()),
            (
                "CONDITIONAL",
                Some(&markets.tokens[0]),
                d::parse("0.000001")?,
            ),
        ] {
            let mut params = vec![
                ("asset_type".into(), kind.into()),
                ("signature_type".into(), self.signature_type.to_string()),
            ];
            if let Some(token) = token {
                params.push(("token_id".into(), token.clone()));
            }
            let raw = self
                .private(0, Method::GET, "/balance-allowance", &params, None)
                .await?;
            let value = raw["allowances"]
                .as_object()
                .context("Missing Polymarket allowances")?
                .iter()
                .find(|(key, _)| key.eq_ignore_ascii_case(spender))
                .context("Missing Polymarket allowance")?
                .1;
            // Raw integer allowance conversion is exact even at uint256 maximum.
            let allowance = &d::amount(value)? / Decimal::from(1_000_000);
            ensure!(
                allowance >= needed,
                "Polymarket allowance/operator approval insufficient"
            );
        }
        let info = &markets.predict_info;
        let spender = Address::from_str(signing::predict_exchange(
            info["neg_risk"] == true,
            info["yield_bearing"] == true,
        ))?;
        let args = (self.predict_address(), spender).abi_encode();
        let (allowance, operator) = tokio::try_join!(
            self.rpc(
                1,
                signing::PREDICT_USDT,
                calldata("allowance(address,address)", args.clone())
            ),
            self.rpc(
                1,
                signing::predict_ctf(info["neg_risk"] == true, info["yield_bearing"] == true),
                calldata("isApprovedForAll(address,address)", args)
            )
        )?;
        ensure!(
            signing::from_raw(allowance, 18)? >= required[1] && operator == U256::from(1),
            "Predict allowance/operator approval insufficient"
        );
        Ok(())
    }
    pub async fn available_shares(
        &self,
        index: usize,
        markets: &Markets,
        side: &str,
    ) -> Result<Decimal> {
        let (owner, contract, token, places) = if index == 0 {
            (
                self.poly_maker,
                signing::POLY_CTF,
                markets.tokens[usize::from(side == "no")].as_str(),
                6,
            )
        } else {
            (
                self.predict_address(),
                signing::predict_ctf(
                    markets.predict_info["neg_risk"] == true,
                    markets.predict_info["yield_bearing"] == true,
                ),
                markets.predict_info[if side == "yes" {
                    "yes_token"
                } else {
                    "no_token"
                }]
                .as_str()
                .context("Missing token")?,
                18,
            )
        };
        let value = self
            .rpc(
                index,
                contract,
                calldata(
                    "balanceOf(address,uint256)",
                    (owner, U256::from_str(token)?).abi_encode(),
                ),
            )
            .await?;
        signing::from_raw(value, places)
    }
}

pub fn calldata(signature: &str, args: Vec<u8>) -> String {
    let mut bytes = keccak256(signature).as_slice()[..4].to_vec();
    bytes.extend_from_slice(&args);
    hex::encode_prefixed(bytes)
}
pub fn shares(value: &Value) -> Result<Decimal> {
    let text = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    if text == "null" || text.is_empty() {
        return Ok(d::zero());
    }
    let amount = d::parse(&text)?;
    if text.len() > 12 && text.bytes().all(|b| b.is_ascii_digit()) {
        d::div(&amount, &d::parse("1000000000000000000")?)
    } else {
        Ok(amount)
    }
}

#[derive(Clone)]
pub struct Receipt {
    pub id: String,
    pub order_id: String,
    pub amount: Decimal,
    pub price: Decimal,
    pub side: String,
    pub settlement: String,
    pub fee: Option<Decimal>,
    pub fee_currency: Option<String>,
}

pub fn merge_receipts(
    rest: Vec<Receipt>,
    ws: Vec<Receipt>,
    predict: bool,
) -> Result<HashMap<String, Receipt>> {
    let mut merged: HashMap<String, Receipt> = HashMap::new();
    for fill in rest.into_iter().chain(ws) {
        ensure!(
            !fill.id.is_empty()
                && !fill.order_id.is_empty()
                && fill.amount >= d::zero()
                && fill.price >= d::zero()
                && fill.price <= d::one(),
            "Invalid receipt"
        );
        let key = if predict {
            format!("{}:{}", fill.id.split(':').next().unwrap(), fill.order_id)
        } else {
            fill.id.clone()
        };
        if let Some(old) = merged.get(&key) {
            ensure!(
                old.order_id == fill.order_id
                    && old.amount == fill.amount
                    && old.price == fill.price
                    && old.side == fill.side,
                "Receipt identity or amounts disagree"
            );
            if matches!(old.settlement.as_str(), "CONFIRMED" | "FAILED") {
                if matches!(fill.settlement.as_str(), "CONFIRMED" | "FAILED") {
                    ensure!(
                        old.settlement == fill.settlement,
                        "Receipt final states disagree"
                    );
                    if old.fee.is_some() && fill.fee.is_some() {
                        ensure!(
                            old.fee == fill.fee && old.fee_currency == fill.fee_currency,
                            "Receipt fees disagree"
                        );
                    }
                    if old.fee.is_none() && fill.fee.is_some() {
                        merged.insert(key, fill);
                    }
                }
                continue;
            }
        }
        merged.insert(key, fill);
    }
    Ok(merged)
}
