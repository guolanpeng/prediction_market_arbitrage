pub mod adapters;
pub mod btc;
pub mod decimal;
pub mod feed;
pub mod journal;
pub mod logging;
pub mod pair;
pub mod polymarket;
pub mod signing;
pub mod trading;

pub fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

#[cfg(test)]
mod tests;
