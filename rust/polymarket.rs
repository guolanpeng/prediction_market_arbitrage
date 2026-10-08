//! Polymarket uses the official Rust SDK for authentication, signing and orders.
use crate::{decimal as d, signing};
use alloy_primitives::{Address, B256, U256};
use alloy_sol_types::{SolStruct, eip712_domain};
use anyhow::{Context, Result, ensure};
use bigdecimal::RoundingMode;
use k256::ecdsa::SigningKey;
use polymarket_client_sdk_v2::{
    auth::{Credentials, ExposeSecret, LocalSigner, Normal, Signer, state::Authenticated},
    clob::{
        Client, Config,
        types::{
            AssetType, OrderPayload, OrderType, OrderV2, SignableOrder, SignatureType, SignedOrder,
            request::{BalanceAllowanceRequest, TradesRequest},
        },
    },
};
use serde_json::{Value, json};
use std::{str::FromStr, time::Duration};

pub struct Polymarket {
    pub client: Client<Authenticated<Normal>>,
    signer: LocalSigner<SigningKey>,
    pub maker: Address,
    pub signature_type: SignatureType,
}
impl Polymarket {
    pub async fn connect(
        key: &str,
        maker: Address,
        signature_type: u8,
        credentials: Option<Credentials>,
    ) -> Result<Self> {
        crate::adapters::init_tls();
        let signer = LocalSigner::<SigningKey>::from_str(key)
            .context("Invalid Polymarket key")?
            .with_chain_id(Some(137));
        let signature_type = match signature_type {
            0 => SignatureType::Eoa,
            1 => SignatureType::Proxy,
            2 => SignatureType::GnosisSafe,
            3 => SignatureType::Poly1271,
            _ => anyhow::bail!("Invalid signature type"),
        };
        let client = Client::new(
            crate::adapters::CLOB,
            Config::builder().use_server_time(true).build(),
        )
        .map_err(|_| anyhow::anyhow!("Polymarket SDK initialization failed"))?;
        let mut auth = client
            .authentication_builder(&signer)
            .signature_type(signature_type);
        if signature_type != SignatureType::Eoa {
            auth = auth.funder(maker);
        }
        if let Some(credentials) = credentials {
            auth = auth.credentials(credentials);
        }
        let client = tokio::time::timeout(Duration::from_secs(30), auth.authenticate())
            .await
            .context("Polymarket authentication timed out")?
            .map_err(|_| anyhow::anyhow!("Polymarket authentication failed"))?;
        Ok(Self {
            client,
            signer,
            maker,
            signature_type,
        })
    }
    pub fn credentials(&self) -> Value {
        let c = self.client.credentials();
        json!({"apiKey":c.key().to_string(),"secret":c.secret().expose_secret(),"passphrase":c.passphrase().expose_secret()})
    }
    pub async fn prepare(
        &self,
        plan: &Value,
        token: &Value,
        sell: bool,
        price: Option<d::Decimal>,
        now: i64,
        salt: u64,
    ) -> Result<(SignedOrder, String)> {
        let token_id = U256::from_str(token["token_id"].as_str().context("Missing token ID")?)?;
        let negative = token["neg_risk"].as_bool().context("Unknown exchange")?;
        self.client.set_neg_risk(token_id, negative);
        let tick = d::amount(&token["tick"])?;
        let (maker_amount, taker_amount) = if sell {
            signing::poly_sell_amounts(
                &d::amount(&plan["target_shares"])?,
                &price.context("Exit price missing")?,
                &tick,
            )?
        } else {
            let places = match d::text(&tick.normalized()).as_str() {
                "0.1" => 3,
                "0.01" => 4,
                "0.001" => 5,
                "0.0001" => 6,
                _ => anyhow::bail!("Unknown tick size"),
            };
            let budget = d::amount(&plan["cash_budget"])?.with_scale_round(2, RoundingMode::Down);
            ensure!(
                budget >= d::one(),
                "Polymarket market buy needs at least $1"
            );
            let shares = d::div(&budget, &d::sub(&d::one(), &tick))?
                .with_scale_round(places, RoundingMode::Down);
            (signing::raw(&budget, 6)?, signing::raw(&shares, 6)?)
        };
        let mut order = OrderV2::default();
        order.salt = U256::from(salt);
        order.maker = self.maker;
        order.signer = if self.signature_type == SignatureType::Poly1271 {
            self.maker
        } else {
            self.signer.address()
        };
        order.tokenId = token_id;
        order.makerAmount = maker_amount;
        order.takerAmount = taker_amount;
        order.side = u8::from(sell);
        order.signatureType = self.signature_type as u8;
        order.timestamp = U256::from(now);
        order.metadata = B256::ZERO;
        order.builder = B256::ZERO;
        let contract = Address::from_str(if negative {
            signing::POLY_NEG_EXCHANGE
        } else {
            signing::POLY_EXCHANGE
        })?;
        let domain = eip712_domain! {name:"Polymarket CTF Exchange",version:"2",chain_id:137,verifying_contract:contract,};
        let hash = format!("{:#x}", order.eip712_signing_hash(&domain));
        let signable = SignableOrder::builder()
            .payload(OrderPayload::new(order, U256::ZERO))
            .order_type(OrderType::FAK)
            .defer_exec(false)
            .build();
        let signed = self
            .client
            .sign(&self.signer, signable)
            .await
            .map_err(|_| anyhow::anyhow!("Polymarket SDK signing failed"))?;
        Ok((signed, hash))
    }
    pub async fn submit(&self, signed: SignedOrder) -> Result<Value> {
        // Exactly one POST. Timeouts and ambiguous errors remain journaled.
        let response =
            tokio::time::timeout(Duration::from_secs(30), self.client.post_order(signed))
                .await
                .context("ORDER_SUBMIT_UNKNOWN")?
                .map_err(|error| {
                    if let Some(status) =
                        error.downcast_ref::<polymarket_client_sdk_v2::error::Status>()
                        && matches!(
                            status.status_code.as_u16(),
                            400 | 401 | 403 | 404 | 409 | 422 | 429
                        )
                    {
                        return anyhow::anyhow!("VENUE_REJECTED");
                    }
                    anyhow::anyhow!("ORDER_SUBMIT_UNKNOWN")
                })?;
        if !response.success || response.error_msg.as_ref().is_some_and(|s| !s.is_empty()) {
            anyhow::bail!("VENUE_REJECTED");
        }
        Ok(json!({"orderID":response.order_id,"success":response.success}))
    }
    pub async fn balance(&self, token: Option<&str>) -> Result<Value> {
        let builder = BalanceAllowanceRequest::builder()
            .asset_type(if token.is_some() {
                AssetType::Conditional
            } else {
                AssetType::Collateral
            })
            .signature_type(self.signature_type);
        // Optional token changes the builder's typestate, so construct separately.
        let request = if let Some(token) = token {
            builder.token_id(U256::from_str(token)?).build()
        } else {
            builder.build()
        };
        let response = tokio::time::timeout(
            Duration::from_secs(30),
            self.client.balance_allowance(request),
        )
        .await
        .context("Polymarket balance timeout")?
        .map_err(|_| anyhow::anyhow!("Polymarket balance read failed"))?;
        let allowances: serde_json::Map<String, Value> = response
            .allowances
            .iter()
            .map(|(key, value)| (key.to_checksum(None), json!(value)))
            .collect();
        Ok(json!({"balance":response.balance.to_string(),"allowances":allowances}))
    }
    pub async fn order(&self, id: &str) -> Result<Value> {
        let r = tokio::time::timeout(Duration::from_secs(30), self.client.order(id))
            .await
            .context("Order read timeout")?
            .map_err(|_| anyhow::anyhow!("Polymarket order read failed"))?;
        Ok(
            json!({"id":r.id,"status":r.status.to_string(),"asset_id":r.asset_id.to_string(),"side":r.side.to_string(),"original_size":r.original_size.to_string(),"size_matched":r.size_matched.to_string(),"price":r.price.to_string(),"associate_trades":r.associate_trades,"order_type":r.order_type.to_string()}),
        )
    }
    pub async fn trades(
        &self,
        condition: &str,
        cursor: Option<String>,
        id: Option<&str>,
    ) -> Result<Value> {
        let builder = TradesRequest::builder().market(B256::from_str(condition)?);
        let req = if let Some(id) = id {
            builder.id(id).build()
        } else {
            builder.build()
        };
        let r = tokio::time::timeout(Duration::from_secs(30), self.client.trades(&req, cursor))
            .await
            .context("Trade read timeout")?
            .map_err(|_| anyhow::anyhow!("Polymarket trade read failed"))?;
        let data:Vec<Value>=r.data.into_iter().map(|r|json!({"id":r.id,"taker_order_id":r.taker_order_id,"asset_id":r.asset_id.to_string(),"side":r.side.to_string(),"size":r.size.to_string(),"price":r.price.to_string(),"status":r.status.to_string(),"trader_side":format!("{:?}",r.trader_side).to_uppercase(),"maker_orders":r.maker_orders.iter().map(|m|json!({"owner":m.owner.to_string(),"maker_address":m.maker_address.to_checksum(None),"order_id":m.order_id,"asset_id":m.asset_id.to_string(),"side":m.side.to_string(),"price":m.price.to_string(),"matched_amount":m.matched_amount.to_string()})).collect::<Vec<_>>()})).collect();
        Ok(json!({"data":data,"next_cursor":r.next_cursor}))
    }
}
