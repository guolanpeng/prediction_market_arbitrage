//! BTC market/account streams. A disconnect invalidates executable books.
use crate::{
    adapters::{self, Clients, Markets, Receipt},
    decimal::{self as d, Decimal},
    pair, trading,
};
use anyhow::{Context, Result, ensure};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    sync::{Mutex, Notify},
    task::JoinHandle,
};
use tokio_tungstenite::{
    connect_async,
    tungstenite::{Message, client::IntoClientRequest},
};

#[derive(Default, Clone)]
pub struct Book {
    pub bids: BTreeMap<Decimal, Decimal>,
    pub asks: BTreeMap<Decimal, Decimal>,
    pub timestamp: i64,
    pub ready: bool,
    pending: Option<(i64, Option<Decimal>, Option<Decimal>, Instant)>,
    verified: Option<Box<Book>>,
    generation: u64,
    received_at: Option<Instant>,
    received_at_ms: Option<i64>,
    verified_at: Option<Instant>,
}
fn number(value: &Value) -> Result<i64> {
    if let Some(value) = value.as_i64() {
        Ok(value)
    } else {
        Ok(value.as_str().context("Missing timestamp")?.parse()?)
    }
}
fn levels(value: &Value) -> Result<BTreeMap<Decimal, Decimal>> {
    let mut rows = BTreeMap::new();
    for level in value.as_array().context("Missing book levels")? {
        let (price, size) = if level.is_object() {
            (d::amount(&level["price"])?, d::amount(&level["size"])?)
        } else {
            (d::amount(&level[0])?, d::amount(&level[1])?)
        };
        if price > d::zero() && price < d::one() && size > d::zero() {
            let old = rows.remove(&price).unwrap_or(d::zero());
            rows.insert(price, d::add(&old, &size));
        }
    }
    Ok(rows)
}
impl Book {
    pub fn replace(&mut self, value: &Value, timestamp: &Value) -> Result<()> {
        self.bids = levels(&value["bids"])?;
        self.asks = levels(&value["asks"])?;
        self.timestamp = number(timestamp)?;
        self.ready = true;
        self.pending = None;
        self.verified = None;
        self.generation = self.generation.wrapping_add(1);
        self.mark_received(Instant::now(), crate::now_ms());
        self.verified_at = Some(Instant::now());
        Ok(())
    }
    fn top_matches(&self, bid: &Option<Decimal>, ask: &Option<Decimal>) -> bool {
        self.bids.last_key_value().map(|(p, _)| p) == bid.as_ref()
            && self.asks.first_key_value().map(|(p, _)| p) == ask.as_ref()
    }
    fn flush(&mut self, force: bool) -> bool {
        if self
            .pending
            .as_ref()
            .is_some_and(|(_, _, _, at)| force || at.elapsed() >= Duration::from_millis(500))
        {
            let (_, bid, ask, _) = self.pending.take().unwrap();
            self.generation = self.generation.wrapping_add(1);
            if !self.top_matches(&bid, &ask) {
                self.ready = false;
                self.verified = None;
                return false;
            }
            self.verified_at = Some(Instant::now());
            self.verified = Some(Box::new(Book {
                bids: self.bids.clone(),
                asks: self.asks.clone(),
                timestamp: self.timestamp,
                ready: true,
                pending: None,
                verified: None,
                generation: self.generation,
                received_at: self.received_at,
                received_at_ms: self.received_at_ms,
                verified_at: self.verified_at,
            }));
        }
        true
    }
    pub fn change(&mut self, row: &Value, timestamp: i64) -> Result<bool> {
        if !self.ready {
            // A snapshot is already requested. Don't flood subscribe requests.
            return Ok(true);
        }
        if timestamp < self.timestamp {
            self.ready = false;
            self.pending = None;
            self.verified = None;
            self.generation = self.generation.wrapping_add(1);
            return Ok(false);
        }
        if self
            .pending
            .as_ref()
            .is_some_and(|(old, _, _, _)| *old != timestamp)
            && !self.flush(true)
        {
            return Ok(false);
        }
        let price = d::amount(&row["price"])?;
        let size = d::amount(&row["size"])?;
        ensure!(
            price > d::zero() && price < d::one() && size >= d::zero(),
            "Invalid book change"
        );
        let book = match row["side"].as_str() {
            Some("BUY") => &mut self.bids,
            Some("SELL") => &mut self.asks,
            _ => anyhow::bail!("Invalid book side"),
        };
        if size == d::zero() {
            book.remove(&price);
        } else {
            book.insert(price, size);
        }
        let top = |value: &Value| -> Result<Option<Decimal>> {
            let price = d::amount(value)?;
            Ok(if price > d::zero() && price < d::one() {
                Some(price)
            } else {
                None
            })
        };
        self.timestamp = timestamp;
        self.pending = Some((
            timestamp,
            top(&row["best_bid"])?,
            top(&row["best_ask"])?,
            Instant::now(),
        ));
        self.mark_received(Instant::now(), crate::now_ms());
        Ok(true)
    }
    fn valid(&self, now: i64) -> bool {
        self.ready
            && self.pending.is_none()
            && self.timestamp > 0
            && (0..=15000).contains(&(now - self.timestamp))
    }
    fn executable(&self, now: i64) -> Option<&Book> {
        if !self.ready {
            return None;
        }
        if self.pending.is_none() {
            return self.valid(now).then_some(self);
        }
        self.verified.as_deref().filter(|book| book.valid(now))
    }
    fn mark_received(&mut self, at: Instant, wall_time: i64) {
        self.received_at = Some(at);
        self.received_at_ms = Some(wall_time);
    }
    fn timing(&self, decision: Instant, wall_time: i64) -> Value {
        let micros = |duration: Duration| duration.as_micros().min(u64::MAX as u128) as u64;
        json!({"source_at_ms":self.timestamp,"received_at_ms":self.received_at_ms,
            "source_age_ms":wall_time-self.timestamp,
            "receive_to_decision_us":self.received_at.map(|at|micros(decision.saturating_duration_since(at))),
            "validation_delay_us":self.received_at.zip(self.verified_at).map(|(received,verified)|micros(verified.saturating_duration_since(received))),
            "validated_to_decision_us":self.verified_at.map(|at|micros(decision.saturating_duration_since(at)))})
    }
    fn asks_json(&self) -> Value {
        json!(
            self.asks
                .iter()
                .map(|(p, s)| vec![d::text(p), d::text(s)])
                .collect::<Vec<_>>()
        )
    }
    fn bids_json(&self) -> Value {
        json!(
            self.bids
                .iter()
                .rev()
                .map(|(p, s)| vec![d::text(p), d::text(s)])
                .collect::<Vec<_>>()
        )
    }
    fn mirror(&self) -> Self {
        let mut out = Self {
            timestamp: self.timestamp,
            ready: self.ready,
            received_at: self.received_at,
            received_at_ms: self.received_at_ms,
            verified_at: self.verified_at,
            ..Self::default()
        };
        for (p, s) in &self.bids {
            out.asks.insert(d::sub(&d::one(), p), s.clone());
        }
        for (p, s) in &self.asks {
            out.bids.insert(d::sub(&d::one(), p), s.clone());
        }
        out
    }
}

#[derive(Default)]
struct State {
    poly: [Book; 2],
    predict: Book,
    connected: [bool; 2],
    user_connected: [bool; 2],
    user_received: [bool; 2],
    orders: HashMap<String, Value>,
    receipts: [HashMap<String, Receipt>; 2],
    conflicts: HashSet<String>,
    halted: bool,
}

#[derive(Default)]
struct UpdateSignal {
    notify: Notify,
    published_at: std::sync::Mutex<Option<Instant>>,
}
impl UpdateSignal {
    fn notify_one(&self) {
        if let Ok(mut at) = self.published_at.lock() {
            *at = Some(Instant::now());
        }
        self.notify.notify_one();
    }
    async fn notified(&self) {
        self.notify.notified().await;
    }
    fn published_at(&self) -> Option<Instant> {
        self.published_at.lock().ok().and_then(|at| *at)
    }
}
pub struct Feeds {
    state: Arc<Mutex<State>>,
    updates: Arc<UpdateSignal>,
    tasks: Vec<JoinHandle<()>>,
}
impl Default for Feeds {
    fn default() -> Self {
        Self {
            state: Arc::new(Mutex::new(State::default())),
            updates: Arc::new(UpdateSignal::default()),
            tasks: Vec::new(),
        }
    }
}
impl Drop for Feeds {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
    }
}
impl Feeds {
    pub fn market(markets: &Markets, api_key: &str) -> Self {
        let mut feed = Self::default();
        let tokens = markets.tokens.clone();
        let id = markets.window["predictfun"]
            .as_str()
            .unwrap_or("")
            .to_owned();
        for index in 0..2 {
            let state = feed.state.clone();
            let tokens = tokens.clone();
            let id = id.clone();
            let key = api_key.to_owned();
            let updates = feed.updates.clone();
            feed.tasks.push(tokio::spawn(async move {
                loop {
                    let result =
                        market_loop(index, state.clone(), updates.clone(), &tokens, &id, &key)
                            .await;
                    {
                        let mut state = state.lock().await;
                        state.connected[index] = false;
                        if index == 0 {
                            for book in &mut state.poly {
                                book.ready = false;
                                book.pending = None;
                            }
                        } else {
                            state.predict.ready = false;
                        }
                    }
                    updates.notify_one();
                    if let Err(error) = result {
                        log::info!(
                            "MARKET_WS_DISCONNECTED venue={} reason={} executable_books=false",
                            if index == 0 {
                                "polymarket"
                            } else {
                                "predict_fun"
                            },
                            stream_error_code(&error)
                        );
                    }
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }));
        }
        feed
    }
    pub async fn watch_orders(&self, orders: &[Value]) -> Result<()> {
        let mut state = self.state.lock().await;
        for order in orders.iter().filter(|o| o.is_object()) {
            state.orders.insert(
                order["id"].as_str().context("Order ID missing")?.to_owned(),
                order.clone(),
            );
        }
        Ok(())
    }
    pub fn users(&mut self, clients: &Clients, markets: &Markets) {
        let context = Arc::new(UserContext {
            window: markets.window.clone(),
            poly: markets.poly.clone(),
            predict: markets.predict.clone(),
            tokens: markets.tokens.clone(),
            configs: markets.configs.clone(),
            info: markets.predict_info.clone(),
            api: clients.poly_api.clone(),
            wallet: clients.poly_maker.to_checksum(None),
            signer: clients.poly_wallet.address.to_checksum(None),
            key: clients.data.api_key.clone(),
            jwt: clients.jwt.clone(),
        });
        for index in 0..2 {
            let state = self.state.clone();
            let context = context.clone();
            self.tasks.push(tokio::spawn(async move {
                loop {
                    let _ = user_loop(index, state.clone(), &context).await;
                    {
                        let mut state = state.lock().await;
                        state.user_connected[index] = false;
                        state.user_received[index] = false;
                    }
                    log::info!(
                        "USER_WS_DISCONNECTED venue={} rest_fallback=true",
                        if index == 0 {
                            "polymarket"
                        } else {
                            "predict_fun"
                        }
                    );
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }));
        }
    }
    pub async fn connected(&self) -> bool {
        let state = self.state.lock().await;
        let now = crate::now_ms();
        !state.halted
            && state.connected.iter().all(|v| *v)
            && state.poly.iter().all(|book| book.executable(now).is_some())
            && state.predict.valid(now)
    }
    pub async fn candidates(&self, markets: &Markets, quantity: &Decimal) -> Result<Vec<Value>> {
        if !markets.poly["orderMinSize"].is_null()
            && quantity < &d::amount(&markets.poly["orderMinSize"])?
        {
            return Ok(Vec::new());
        }
        let state = self.state.lock().await;
        let compute_started = Instant::now();
        let published_at = self.updates.published_at();
        let now = crate::now_ms();
        if state.halted
            || !state.connected.iter().all(|v| *v)
            || !state.poly.iter().all(|b| b.executable(now).is_some())
            || !state.predict.valid(now)
        {
            return Ok(Vec::new());
        }
        let predict = [state.predict.clone(), state.predict.mirror()];
        let mut results = Vec::new();
        for (a, b) in [(0, 1), (1, 0)] {
            let poly = state.poly[a]
                .executable(now)
                .context("Verified book missing")?;
            let asks_a = poly.asks_json();
            let asks_b = predict[b].asks_json();
            let result = pair::calculate(
                &json!({"asks_a":asks_a,"asks_b":asks_b,"quantity":d::json(quantity),"configs":markets.configs}),
            )?;
            let tick = markets.predict["decimalPrecision"]
                .as_u64()
                .filter(|p| (1..=18).contains(p))
                .map(|p| format!("0.{}1", "0".repeat((p - 1) as usize)));
            let decision = Instant::now();
            let wall_time = crate::now_ms();
            results.push(json!({"sides":[if a==0{"yes"}else{"no"},if b==0{"yes"}else{"no"}],"asks":[asks_a,asks_b],"result":result,"timestamps":[poly.timestamp,predict[b].timestamp],"configs":markets.configs,"ticks":[markets.poly["orderPriceMinTickSize"],tick],"decision_at_ms":wall_time,"signal_to_decision_us":published_at.map(|at|decision.saturating_duration_since(at).as_micros().min(u64::MAX as u128) as u64),"decision_compute_us":decision.saturating_duration_since(compute_started).as_micros().min(u64::MAX as u128) as u64,"timing":[poly.timing(decision,wall_time),predict[b].timing(decision,wall_time)]}));
        }
        results.sort_by(|a, b| {
            d::amount(&b["result"]["net"])
                .unwrap_or(d::zero())
                .cmp(&d::amount(&a["result"]["net"]).unwrap_or(d::zero()))
        });
        Ok(results)
    }
    pub async fn bids(&self, index: usize, side: &str) -> Value {
        let state = self.state.lock().await;
        if state.halted || !state.connected[index] {
            return json!([]);
        }
        let book = if index == 0 {
            state.poly[usize::from(side == "no")].clone()
        } else if side == "no" {
            state.predict.mirror()
        } else {
            state.predict.clone()
        };
        let Some(book) = book.executable(crate::now_ms()) else {
            return json!([]);
        };
        book.bids_json()
    }
    pub async fn receipts(&self) -> [Vec<Receipt>; 2] {
        let state = self.state.lock().await;
        [
            state.receipts[0]
                .values()
                .filter(|r| !state.conflicts.contains(&r.order_id))
                .cloned()
                .collect(),
            state.receipts[1]
                .values()
                .filter(|r| !state.conflicts.contains(&r.order_id))
                .cloned()
                .collect(),
        ]
    }
    pub async fn conflicts(&self) -> bool {
        !self.state.lock().await.conflicts.is_empty()
    }
    pub async fn users_ready(&self) -> bool {
        let state = self.state.lock().await;
        state.user_connected.iter().all(|v| *v) && state.user_received.iter().all(|v| *v)
    }
    pub async fn wait_update(&self, timeout: Duration) -> bool {
        tokio::time::timeout(timeout, self.updates.notified())
            .await
            .is_ok()
    }
}

fn stream_error_code(error: &anyhow::Error) -> &'static str {
    if error.downcast_ref::<serde_json::Error>().is_some() {
        return "FRAME_JSON_INVALID";
    }
    if let Some(error) = error.downcast_ref::<tokio_tungstenite::tungstenite::Error>() {
        return match error {
            tokio_tungstenite::tungstenite::Error::Http(_) => "HANDSHAKE_HTTP",
            tokio_tungstenite::tungstenite::Error::Tls(_) => "TLS",
            tokio_tungstenite::tungstenite::Error::Io(_) => "TRANSPORT_IO",
            tokio_tungstenite::tungstenite::Error::Protocol(_) => "WS_PROTOCOL",
            tokio_tungstenite::tungstenite::Error::ConnectionClosed
            | tokio_tungstenite::tungstenite::Error::AlreadyClosed => "CONNECTION_CLOSED",
            _ => "WS_TRANSPORT",
        };
    }
    match error.to_string().as_str() {
        "Missing timestamp" => "TIMESTAMP_MISSING",
        "Missing book levels" => "BOOK_LEVELS_MISSING",
        "Invalid decimal amount" => "AMOUNT_INVALID",
        "Invalid book change" => "BOOK_CHANGE_INVALID",
        "Out-of-order book change" => "BOOK_CHANGE_OUT_OF_ORDER",
        "Invalid book side" => "BOOK_SIDE_INVALID",
        "Tick size changed; reload window metadata" => "TICK_SIZE_CHANGED",
        "Market heartbeat timeout" => "HEARTBEAT_TIMEOUT",
        "Market stream closed" => "STREAM_CLOSED",
        "Subscription refused" => "SUBSCRIPTION_REFUSED",
        "Invalid price changes" => "BOOK_CHANGES_INVALID",
        _ => "STREAM_DATA_INVALID",
    }
}

async fn market_loop(
    index: usize,
    state: Arc<Mutex<State>>,
    updates: Arc<UpdateSignal>,
    tokens: &[String; 2],
    id: &str,
    key: &str,
) -> Result<()> {
    let mut req = if index == 0 {
        "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    } else {
        "wss://ws.predict.fun/ws"
    }
    .into_client_request()?;
    if index == 1 {
        req.headers_mut().insert("x-api-key", key.parse()?);
    }
    let (mut ws, _) = tokio::time::timeout(Duration::from_secs(15), connect_async(req)).await??;
    if index == 0 {
        ws.send(Message::Text(
            json!({"assets_ids":tokens,"type":"market","custom_feature_enabled":true})
                .to_string()
                .into(),
        ))
        .await?;
    } else {
        for (n, kind) in [
            "predictOrderbook",
            "predictMarketStatus",
            "predictTradingStatus",
        ]
        .iter()
        .enumerate()
        {
            ws.send(Message::Text(
                json!({"method":"subscribe","requestId":n+1,"params":[format!("{kind}/{id}")]})
                    .to_string()
                    .into(),
            ))
            .await?;
        }
    }
    state.lock().await.connected[index] = true;
    let mut tick = tokio::time::interval(Duration::from_millis(500));
    let mut ping = Instant::now();
    let mut received = Instant::now();
    loop {
        tokio::select! {
            _=tick.tick()=>{
                ensure!(received.elapsed()<Duration::from_secs(35),"Market heartbeat timeout");
                if index==0{
                    if ping.elapsed()>=Duration::from_secs(10){ws.send(Message::Text("PING".into())).await?;ping=Instant::now();}
                    let mut state=state.lock().await;for (i,book) in state.poly.iter_mut().enumerate(){let generation=book.generation;let valid=book.flush(false);if book.generation!=generation{updates.notify_one();}
                    if !valid{ws.send(Message::Text(json!({"assets_ids":[tokens[i]],"operation":"unsubscribe"}).to_string().into())).await?;ws.send(Message::Text(json!({"assets_ids":[tokens[i]],"operation":"subscribe"}).to_string().into())).await?;}}
                }
            }
            frame=ws.next()=>{
                let frame=frame.context("Market stream closed")??;received=Instant::now();let frame_at_ms=crate::now_ms();
                if let Message::Ping(data)=frame{ws.send(Message::Pong(data)).await?;continue;}
                let Message::Text(text)=frame else{continue;};if text=="PONG"{continue;}
                let raw:Value=serde_json::from_str(&text)?;
                if index==1&&raw["topic"]=="heartbeat"{ws.send(Message::Text(json!({"method":"heartbeat","data":raw["data"]}).to_string().into())).await?;continue;}
                if index==0{
                    let rows=if let Some(rows)=raw.as_array(){rows.clone()}else{vec![raw]};
                    for row in rows{
                        if row["event_type"]=="market_resolved" && (tokens.iter().any(|t|row["winning_asset_id"]==*t||row["asset_id"]==*t)||row["assets_ids"].as_array().is_some_and(|ids|tokens.iter().any(|t|ids.contains(&json!(t))))){state.lock().await.halted=true;continue;}
                        if row["event_type"]=="tick_size_change" && tokens.iter().any(|t|row["asset_id"]==*t){state.lock().await.halted=true;return Err(anyhow::anyhow!("Tick size changed; reload window metadata"));}
                        if row["event_type"]=="book" && let Some(index)=tokens.iter().position(|t|row["asset_id"]==*t){let mut state=state.lock().await;state.poly[index].replace(&row,&row["timestamp"])?;state.poly[index].mark_received(received,frame_at_ms);updates.notify_one();}
                        if row["event_type"]=="price_change"{let stamp=number(&row["timestamp"])?;for change in row["price_changes"].as_array().context("Invalid price changes")?{if let Some(index)=tokens.iter().position(|t|change["asset_id"]==*t){let mut state=state.lock().await;let generation=state.poly[index].generation;let valid=state.poly[index].change(change,stamp)?;state.poly[index].mark_received(received,frame_at_ms);if generation!=state.poly[index].generation{updates.notify_one();}
                    if !valid{ws.send(Message::Text(json!({"assets_ids":[tokens[index]],"operation":"unsubscribe"}).to_string().into())).await?;ws.send(Message::Text(json!({"assets_ids":[tokens[index]],"operation":"subscribe"}).to_string().into())).await?;}}}}
                    }
                }else{
                    let topic=raw["topic"].as_str().unwrap_or("");
                    if topic==format!("predictOrderbook/{id}"){let mut state=state.lock().await;state.predict.replace(&raw["data"],&raw["data"]["updateTimestampMs"])?;state.predict.mark_received(received,frame_at_ms);updates.notify_one();}
                    if topic==format!("predictTradingStatus/{id}")&&raw["data"]["tradingStatus"]!="OPEN"{state.lock().await.halted=true;}
                    if topic==format!("predictMarketStatus/{id}")&& !matches!(raw["data"]["status"].as_str(),Some("OPEN"|"REGISTERED"|"UNPAUSED")){state.lock().await.halted=true;}
                    if raw.get("error").is_some_and(|error| !error.is_null()) || raw["success"] == false {return Err(anyhow::anyhow!("Subscription refused"));}
                }
            }
        }
    }
}

struct UserContext {
    window: Value,
    poly: Value,
    predict: Value,
    tokens: [String; 2],
    configs: [Value; 2],
    info: Value,
    api: Value,
    wallet: String,
    signer: String,
    key: String,
    jwt: String,
}
async fn user_loop(index: usize, state: Arc<Mutex<State>>, context: &UserContext) -> Result<()> {
    let mut req = if index == 0 {
        "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    } else {
        "wss://ws.predict.fun/ws"
    }
    .into_client_request()?;
    if index == 1 {
        req.headers_mut().insert("x-api-key", context.key.parse()?);
    }
    let (mut ws, _) = tokio::time::timeout(Duration::from_secs(15), connect_async(req)).await??;
    let sub = if index == 0 {
        json!({"type":"user","markets":[context.poly["conditionId"]],"auth":{"apiKey":context.api["apiKey"],"secret":context.api["secret"],"passphrase":context.api["passphrase"]}})
    } else {
        json!({"method":"subscribe","requestId":1,"params":[format!("predictWalletEvents/{}",context.jwt)]})
    };
    ws.send(Message::Text(sub.to_string().into())).await?;
    state.lock().await.user_connected[index] = true;
    let markets = Markets {
        window: context.window.clone(),
        poly: context.poly.clone(),
        predict: context.predict.clone(),
        tokens: context.tokens.clone(),
        configs: context.configs.clone(),
        predict_info: context.info.clone(),
    };
    let mut tick = tokio::time::interval(Duration::from_secs(10));
    let mut received = Instant::now();
    loop {
        tokio::select! {
            _=tick.tick()=>{ensure!(received.elapsed()<Duration::from_secs(35),"User heartbeat timeout");if index==0{ws.send(Message::Text("PING".into())).await?;}}
            frame=ws.next()=>{
            let frame=frame.context("User stream closed")??;received=Instant::now();
            state.lock().await.user_received[index]=true;
                if let Message::Ping(data)=frame{ws.send(Message::Pong(data)).await?;continue;}
                let Message::Text(text)=frame else{continue;};if text=="PONG"{continue;}
                let raw:Value=serde_json::from_str(&text)?;
                if index==1&&raw["topic"]=="heartbeat"{ws.send(Message::Text(json!({"method":"heartbeat","data":raw["data"]}).to_string().into())).await?;continue;}
                let mut state=state.lock().await;
                let mut fills=Vec::new();
                if index==0&&raw["event_type"]=="trade"{
                    for order in state.orders.values(){if order["venue"]=="polymarket"{fills.extend(trading::poly_receipts(&raw,order,&markets,context.api["apiKey"].as_str().context("Missing API key")?,&context.wallet,&context.signer)?);}}
                }
                if index==1&&raw["topic"].as_str().is_some_and(|t|t.starts_with("predictWalletEvents/")){
                    let event=&raw["data"];let id=event["orderHash"].as_str().or(event["orderId"].as_str()).unwrap_or("");
                    if state.orders.contains_key(id)&&event["details"]["marketId"].to_string().trim_matches('"')==context.window["predictfun"].as_str().unwrap_or(""){
                        let settlement=match event["type"].as_str(){Some("orderTransactionSubmitted")=>"MATCHED",Some("orderTransactionSuccess")=>"CONFIRMED",Some("orderTransactionFailed")=>"FAILED",_=>continue};
                        let fill=&event["fill"];if fill.is_null(){continue;}
                        let price=if fill["executedPriceWei"].is_null(){d::amount(&event["details"]["price"])?}else{d::div(&d::amount(&fill["executedPriceWei"])?,&d::parse("1000000000000000000")?)?};
                    let order=state.orders.get(id).context("Unknown observed order")?;
                    let outcome=event["details"]["outcome"].as_str().context("Missing fill outcome")?;
                    let yes=(order["side"]=="buy")!=(order["reduce_only"]==true);
                    ensure!((outcome.eq_ignore_ascii_case("YES")&&yes)||(outcome.eq_ignore_ascii_case("NO")&&!yes),"WS fill outcome disagrees");
                    let quote=event["details"]["quoteType"].as_str().context("Missing quote type")?;
                    ensure!(quote.eq_ignore_ascii_case("BID")||quote.eq_ignore_ascii_case("ASK"),"Invalid WS quote type");
                    let fee=&event["fee"];
                        fills.push(Receipt{id:event["settlementId"].as_str().unwrap_or(id).into(),order_id:id.into(),amount:d::div(&d::amount(&fill["executedSizeWei"])?,&d::parse("1000000000000000000")?)?,price,side:if event["details"]["quoteType"].as_str().unwrap_or("").eq_ignore_ascii_case("BID"){"BUY"}else{"SELL"}.into(),settlement:settlement.into(),fee:if fee["amountWei"].is_null(){None}else{Some(d::div(&d::amount(&fee["amountWei"])?,&d::parse("1000000000000000000")?)?)},fee_currency:if fee["type"]=="COLLATERAL"{Some("USDT".into())}else if fee.is_object()&&!fee.as_object().unwrap().is_empty(){Some("shares".into())}else{None}});
                    }
                }
                for fill in fills{let id=fill.order_id.clone();match adapters::merge_receipts(state.receipts[index].values().cloned().collect(),vec![fill],index==1){Ok(merged)=>state.receipts[index]=merged,Err(_)=>{state.conflicts.insert(id);}}}
                if raw.get("error").is_some_and(|error| !error.is_null()) || raw["success"] == false {return Err(anyhow::anyhow!("User subscription refused"));}
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mirrors_and_checks_same_timestamp_change_group() {
        let mut book = Book::default();
        book.replace(
            &json!({"bids":[["0.4","10"]],"asks":[["0.6","10"]]}),
            &json!(1000),
        )
        .unwrap();
        let mirror = book.mirror();
        assert_eq!(mirror.asks_json(), json!([["0.6", "10"]]));
        let row = json!({"side":"BUY","price":"0.5","size":"3","best_bid":"0.5","best_ask":"0.7"});
        book.change(&row, 1100).unwrap();
        assert!(book.pending.is_some());
        let row = json!({"side":"SELL","price":"0.6","size":"0","best_bid":"0.5","best_ask":"0.7"});
        book.change(&row, 1100).unwrap();
        let row = json!({"side":"SELL","price":"0.7","size":"8","best_bid":"0.5","best_ask":"0.7"});
        book.change(&row, 1100).unwrap();
        assert!(book.flush(true));
        assert!(book.ready);
        book.change(
            &json!({"side":"BUY","price":"0.5","size":"1","best_bid":"0.8","best_ask":"0.7"}),
            1200,
        )
        .unwrap();
        assert!(!book.flush(true));
        assert!(!book.ready);
    }

    #[test]
    fn out_of_order_delta_requests_one_snapshot_without_reconnecting() {
        let mut book = Book::default();
        book.replace(
            &json!({"bids":[["0.4","10"]],"asks":[["0.6","10"]]}),
            &json!(1000),
        )
        .unwrap();
        let row = json!({"side":"BUY","price":"0.4","size":"9","best_bid":"0.4","best_ask":"0.6"});
        assert!(!book.change(&row, 999).unwrap());
        assert!(!book.ready);
        assert!(book.executable(1000).is_none());
        assert!(book.change(&row, 998).unwrap());
        assert!(!book.ready);
        book.replace(
            &json!({"bids":[["0.4","9"]],"asks":[["0.6","10"]]}),
            &json!(1010),
        )
        .unwrap();
        assert!(book.executable(1010).is_some());
    }

    #[test]
    fn continuously_changing_books_expose_only_verified_snapshots() {
        let mut book = Book::default();
        book.replace(
            &json!({"bids":[["0.4","10"]],"asks":[["0.6","10"]]}),
            &json!(1000),
        )
        .unwrap();
        book.change(
            &json!({"side":"BUY","price":"0.4","size":"9","best_bid":"0.4","best_ask":"0.6"}),
            1100,
        )
        .unwrap();
        assert!(book.executable(1100).is_none());
        book.change(
            &json!({"side":"BUY","price":"0.4","size":"8","best_bid":"0.4","best_ask":"0.6"}),
            1200,
        )
        .unwrap();
        let verified = book.executable(1200).unwrap();
        assert_eq!(verified.timestamp, 1100);
        assert_eq!(verified.bids_json(), json!([["0.4", "9"]]));
        assert_eq!(book.bids_json(), json!([["0.4", "8"]]));
        book.change(
            &json!({"side":"BUY","price":"0.4","size":"7","best_bid":"0.5","best_ask":"0.6"}),
            1300,
        )
        .unwrap();
        assert!(!book.flush(true));
        assert!(book.executable(1300).is_none());
    }

    #[tokio::test]
    async fn update_before_wait_is_not_lost_and_does_not_wait_for_poll_interval() {
        let feed = Feeds::default();
        feed.updates.notify_one();
        assert!(
            tokio::time::timeout(
                Duration::from_millis(100),
                feed.wait_update(Duration::from_secs(2))
            )
            .await
            .unwrap()
        );
        assert!(!feed.wait_update(Duration::from_millis(5)).await);
    }

    #[tokio::test]
    async fn multiple_updates_coalesce_instead_of_queueing_old_decisions() {
        let feed = Feeds::default();
        for _ in 0..100 {
            feed.updates.notify_one();
        }
        assert!(feed.wait_update(Duration::from_millis(100)).await);
        assert!(!feed.wait_update(Duration::from_millis(5)).await);
    }

    #[tokio::test]
    async fn data_arrival_wakes_a_waiting_decision() {
        let feed = Arc::new(Feeds::default());
        let waiter = feed.clone();
        let task = tokio::spawn(async move { waiter.wait_update(Duration::from_secs(2)).await });
        tokio::task::yield_now().await;
        feed.updates.notify_one();
        assert!(
            tokio::time::timeout(Duration::from_millis(100), task)
                .await
                .unwrap()
                .unwrap()
        );
    }

    #[test]
    fn unverified_deltas_do_not_advance_publish_generation() {
        let mut book = Book::default();
        book.replace(
            &json!({"bids":[["0.4","10"]],"asks":[["0.6","10"]]}),
            &json!(1000),
        )
        .unwrap();
        let generation = book.generation;
        book.change(
            &json!({"side":"BUY","price":"0.4","size":"9","best_bid":"0.4","best_ask":"0.6"}),
            1100,
        )
        .unwrap();
        assert_eq!(book.generation, generation);
        assert!(book.executable(1100).is_none());
        assert!(book.flush(true));
        assert_ne!(book.generation, generation);
        let timing = book.timing(Instant::now(), crate::now_ms());
        assert!(timing["received_at_ms"].is_i64());
        assert!(timing["receive_to_decision_us"].is_u64());
        assert!(timing["validation_delay_us"].is_u64());
        assert!(timing["validated_to_decision_us"].is_u64());
    }
}
