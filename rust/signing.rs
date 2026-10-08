//! Native EIP-712 signing, matching the pinned Python payloads.
use crate::decimal::{self as d, Decimal};
use alloy_primitives::{Address, B256, U256, hex, keccak256};
use alloy_sol_types::{SolStruct, eip712_domain, sol};
use anyhow::{Context, Result, ensure};
use bigdecimal::RoundingMode;
use k256::ecdsa::SigningKey;
use num_bigint::BigInt;
use serde_json::{Value, json};
use std::str::FromStr;

pub const POLY_EXCHANGE: &str = "0xE111180000d2663C0091e4f400237545B87B996B";
pub const POLY_NEG_EXCHANGE: &str = "0xe2222d279d744050d28e00520010520000310F59";
pub const POLY_CTF: &str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
pub const PREDICT_USDT: &str = "0x55d398326f99059fF775485246999027B3197955";
mod predict {
    use super::*;
    sol! { struct Order { uint256 salt; address maker; address signer; address taker; uint256 tokenId; uint256 makerAmount; uint256 takerAmount; uint256 expiration; uint256 nonce; uint256 feeRateBps; uint8 side; uint8 signatureType; }
    struct Kernel { bytes32 hash; } }
}

pub struct Wallet {
    key: SigningKey,
    pub address: Address,
}
impl Wallet {
    pub fn new(key: &str) -> Result<Self> {
        let key = SigningKey::from_slice(&hex::decode(key).context("Invalid private key")?)
            .context("Invalid private key")?;
        let point = key.verifying_key().to_encoded_point(false);
        let hash = keccak256(&point.as_bytes()[1..]);
        Ok(Self {
            key,
            address: Address::from_slice(&hash[12..]),
        })
    }
    pub fn sign(&self, digest: B256) -> Result<Vec<u8>> {
        let (signature, id) = self
            .key
            .sign_prehash_recoverable(digest.as_slice())
            .context("Signing failed")?;
        let mut bytes = signature.to_bytes().to_vec();
        bytes.push(27 + id.to_byte());
        Ok(bytes)
    }
    fn account_signature(&self, digest: B256, account: Address) -> Result<String> {
        let domain = eip712_domain! { name: "Kernel", version: "0.3.1", chain_id: 56, verifying_contract: account, };
        let wrapped = predict::Kernel { hash: digest }.eip712_signing_hash(&domain);
        let mut bytes = vec![1];
        bytes.extend_from_slice(
            Address::from_str("0x845ADb2C711129d4f3966735eD98a9F09fC4cE57")?.as_slice(),
        );
        bytes.extend_from_slice(&self.sign(personal(wrapped.as_slice()))?);
        Ok(hex::encode_prefixed(bytes))
    }
    pub fn login(&self, message: &str, account: Option<Address>) -> Result<String> {
        let digest = personal(message.as_bytes());
        if let Some(account) = account {
            self.account_signature(digest, account)
        } else {
            Ok(hex::encode_prefixed(self.sign(digest)?))
        }
    }
}

fn personal(bytes: &[u8]) -> B256 {
    let mut message = format!("\x19Ethereum Signed Message:\n{}", bytes.len()).into_bytes();
    message.extend_from_slice(bytes);
    keccak256(message)
}

pub fn raw(value: &Decimal, places: u32) -> Result<U256> {
    ensure!(value >= &d::zero(), "Negative amount");
    let value = (value * Decimal::from(10u64.pow(places))).with_scale_round(0, RoundingMode::Down);
    U256::from_str(&value.to_plain_string()).context("Amount exceeds uint256")
}
pub fn from_raw(value: U256, places: i64) -> Result<Decimal> {
    Ok(Decimal::new(BigInt::from_str(&value.to_string())?, places))
}
pub fn significant(value: U256, digits: usize) -> U256 {
    let len = value.to_string().len();
    if len <= digits {
        return value;
    }
    let factor = U256::from(10).pow(U256::from(len - digits));
    value / factor * factor
}
pub fn predict_exchange(negative: bool, yield_bearing: bool) -> &'static str {
    match (negative, yield_bearing) {
        (false, false) => "0x8BC070BEdAB741406F4B1Eb65A72bee27894B689",
        (true, false) => "0x365fb81bd4A24D6303cd2F19c349dE6894D8d58A",
        (false, true) => "0x6bEb5a40C032AFc305961162d8204CDA16DECFa5",
        (true, true) => "0x8A289d458f5a134bA40015085A8F50Ffb681B41d",
    }
}
pub fn predict_ctf(negative: bool, yield_bearing: bool) -> &'static str {
    match (negative, yield_bearing) {
        (_, false) => "0x22DA1810B194ca018378464a58f6Ac2B10C9d244",
        (false, true) => "0x9400F8Ad57e9e0F352345935d6D3175975eb1d9F",
        (true, true) => "0xF64b0b318AAf83BD9071110af24D24445719A07F",
    }
}

pub fn predict_payload(
    plan: &Value,
    info: &Value,
    wallet: &Wallet,
    account: Option<Address>,
    now: i64,
    salt: u64,
) -> Result<(Value, String)> {
    let budget = d::amount(&plan["cash_budget"])?;
    let shares = significant(raw(&d::amount(&plan["target_shares"])?, 18)?, 5);
    let cash = raw(&budget, 18)?;
    ensure!(
        d::amount(&plan["quoted_cash_budget"])? >= d::one()
            && shares >= U256::from(10).pow(U256::from(16))
            && cash < shares,
        "Invalid Predict budget or shares"
    );
    let (mut payload, hash) = predict_order(
        wallet,
        account,
        info,
        plan["outcome"].as_str().context("Missing outcome")?,
        cash,
        shares,
        0,
        now,
        salt,
    )?;
    payload["data"]["pricePerShare"] =
        json!(raw(&d::amount(&plan["expected_price"])?, 18)?.to_string());
    payload["data"]["slippageBps"] = json!("100");
    payload["data"]["isMinAmountOut"] = json!(false);
    Ok((payload, hash))
}

#[allow(clippy::too_many_arguments)]
pub fn predict_order(
    wallet: &Wallet,
    account: Option<Address>,
    info: &Value,
    outcome: &str,
    maker_amount: U256,
    taker_amount: U256,
    side: u8,
    now: i64,
    salt: u64,
) -> Result<(Value, String)> {
    ensure!(matches!(outcome, "yes" | "no"), "Invalid outcome");
    let maker = account.unwrap_or(wallet.address);
    let token = info[if outcome == "yes" {
        "yes_token"
    } else {
        "no_token"
    }]
    .as_str()
    .context("Missing token")?;
    let fee = info["fee_rate_bps"].as_u64().context("Missing fee rate")?;
    let order = predict::Order {
        salt: U256::from(salt),
        maker,
        signer: maker,
        taker: Address::ZERO,
        tokenId: U256::from_str(token)?,
        makerAmount: maker_amount,
        takerAmount: taker_amount,
        expiration: U256::from(now + 300),
        nonce: U256::ZERO,
        feeRateBps: U256::from(fee),
        side,
        signatureType: 0,
    };
    let contract = Address::from_str(predict_exchange(
        info["neg_risk"] == true,
        info["yield_bearing"] == true,
    ))?;
    let domain = eip712_domain! {name:"predict.fun CTF Exchange",version:"1",chain_id:56,verifying_contract:contract,};
    let digest = order.eip712_signing_hash(&domain);
    let signature = if let Some(account) = account {
        wallet.account_signature(digest, account)?
    } else {
        hex::encode_prefixed(wallet.sign(digest)?)
    };
    let hash = format!("{digest:#x}");
    let wire = json!({"salt":salt.to_string(),"maker":maker.to_checksum(None),"signer":maker.to_checksum(None),"taker":Address::ZERO.to_checksum(None),"tokenId":token,"makerAmount":maker_amount.to_string(),"takerAmount":taker_amount.to_string(),"expiration":now+300,"nonce":"0","feeRateBps":fee.to_string(),"side":side,"signatureType":0,"hash":hash,"signature":signature});
    let price = if side == 0 {
        maker_amount * U256::from(10).pow(U256::from(18)) / taker_amount
    } else {
        taker_amount * U256::from(10).pow(U256::from(18)) / maker_amount
    };
    Ok((
        json!({"data":{"order":wire,"strategy":"MARKET","pricePerShare":price.to_string(),"slippageBps":"0"}}),
        hash,
    ))
}

pub fn salt(predict: bool) -> u64 {
    let bytes = *uuid::Uuid::new_v4().as_bytes();
    let value = u64::from_le_bytes(bytes[..8].try_into().unwrap());
    if predict {
        value % 2_147_483_649
    } else {
        value % 9_007_199_254_740_991
    }
}

pub fn poly_sell_amounts(
    quantity: &Decimal,
    price: &Decimal,
    tick: &Decimal,
) -> Result<(U256, U256)> {
    let amount_places = match d::text(&tick.normalized()).as_str() {
        "0.1" => 3,
        "0.01" => 4,
        "0.001" => 5,
        "0.0001" => 6,
        _ => anyhow::bail!("Invalid tick"),
    };
    let size = quantity.with_scale_round(2, RoundingMode::Down);
    ensure!(
        size > d::zero() && price > &d::zero() && price < &d::one(),
        "Invalid exit size or price"
    );
    let places = tick.normalized().as_bigint_and_exponent().1;
    let price = price.with_scale_round(places, RoundingMode::HalfUp);
    let value = (&size * price).with_scale_round(amount_places, RoundingMode::Down);
    Ok((raw(&size, 6)?, raw(&value, 6)?))
}
