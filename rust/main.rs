use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use prediction_market::{
    adapters::{self, Clients, MarketData},
    btc, decimal as d,
    feed::Feeds,
    journal::{AccountLocks, Journal},
    pair, signing, trading,
};
use serde_json::{Value, json};
use std::{
    env,
    io::{self, Read},
    path::PathBuf,
    time::{Duration, Instant},
};

#[derive(Parser)]
#[command(
    name = "prediction-market",
    about = "BTC five-minute paired trading; orders require btc --live"
)]
struct Args {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// BTC feed checks, account preflight, or explicitly enabled live trading.
    Btc(BtcArgs),
    /// Pure offline JSON input; no environment, network, journals or orders.
    Offline {
        #[arg(long)]
        operation: String,
    },
}
#[derive(clap::Args)]
struct BtcArgs {
    #[arg(long, default_value = ".env")]
    env_file: PathBuf,
    #[arg(long)]
    live: bool,
    #[arg(long)]
    preflight_only: bool,
    #[arg(long, requires = "preflight_only")]
    check_user_ws: bool,
    #[arg(long)]
    trade_credentials: bool,
    #[arg(long, default_value = "10")]
    quantity: String,
    #[arg(long, default_value_t = 1)]
    max_trades: u32,
    #[arg(
        long,
        default_value_t = 2.0,
        help = "Minimum interval between CHECK logs; decisions are driven by verified book updates"
    )]
    interval: f64,
    #[arg(long, default_value_t = 0.0)]
    seconds: f64,
}

fn main() {
    let args = Args::parse();
    let result = match args.command {
        Command::Offline { operation } => offline(&operation),
        Command::Btc(args) => {
            // Environment is loaded before creating threads or the async runtime.
            if args.env_file.exists() && dotenvy::from_path(&args.env_file).is_err() {
                eprintln!("Invalid environment file");
                std::process::exit(1);
            }
            if !args.trade_credentials && prediction_market::logging::init(args.live).is_err() {
                eprintln!("Local log initialization failed");
                std::process::exit(1);
            }
            tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .map_err(anyhow::Error::from)
                .and_then(|runtime| runtime.block_on(run(args)))
        }
    };
    if let Err(error) = result {
        // Emit local diagnostics only. SDK/provider error chains may contain secrets.
        let message = error.to_string();
        if message.starts_with("Missing configuration:")
            || message.contains("journal")
            || message.starts_with("Live target")
            || message.starts_with("Intervals")
        {
            eprintln!("{message}");
        } else {
            eprintln!(
                "BTC_RUN_FAILED; inspect local validation and unresolved journal before restarting"
            );
        }
        std::process::exit(1);
    }
}

fn offline(operation: &str) -> Result<()> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let input: Value = serde_json::from_str(&input)?;
    let output = match operation {
        "calculate" => pair::calculate(&input)?,
        "plans" => json!(trading::plans(
            &input["candidate"],
            &d::amount(&input["quantity"])?,
            &input["token"],
            &input["window"]
        )?),
        "window" => btc::validate_window(
            &input["poly"],
            &input["category"],
            input["start"].as_i64().context("Missing start")?,
        )?,
        "settlement" => json!(
            btc::settlement_factors(
                &input["poly"],
                &input["predict"],
                input["sides"].as_array().context("Missing sides")?,
                &input["clob"]
            )?
            .map(|v| v.iter().map(d::json).collect::<Vec<_>>())
        ),
        "predict-sign" => {
            let wallet = signing::Wallet::new(
                input["private_key"]
                    .as_str()
                    .context("Missing offline test key")?,
            )?;
            let account = input["account"].as_str().map(str::parse).transpose()?;
            let (body, id) = signing::predict_payload(
                &input["plan"],
                &input["info"],
                &wallet,
                account,
                input["now_s"].as_i64().context("Missing fixed time")?,
                input["salt"].as_u64().context("Missing fixed salt")?,
            )?;
            json!({"payload":body,"hash":id})
        }
        _ => anyhow::bail!("Unknown offline operation"),
    };
    println!("{}", output);
    Ok(())
}

async fn run(args: BtcArgs) -> Result<()> {
    let quantity = d::parse(&args.quantity)?;
    ensure!(
        quantity > d::zero() && (!args.live || quantity <= 10),
        "Live target shares must be >0 and <=10"
    );
    ensure!(
        args.interval.is_finite()
            && args.interval >= 0.2
            && args.seconds.is_finite()
            && args.seconds >= 0.0
            && args.max_trades > 0,
        "Intervals/max-trades must be positive"
    );
    ensure!(
        !args.preflight_only || args.live,
        "Preflight requires --live"
    );
    if args.trade_credentials {
        let names = [
            "POLYMARKET_PRIVATE_KEY",
            "POLYMARKET_SIGNATURE_TYPE",
            "POLYMARKET_FUNDER",
            "PREDICT_FUN_PRIVATE_KEY",
            "PREDICT_FUN_API_KEY",
            "PREDICT_FUN_ACCOUNT_ADDRESS",
        ];
        let status: serde_json::Map<String, Value> = names
            .iter()
            .map(|name| {
                (
                    (*name).into(),
                    json!(env::var(name).is_ok_and(|v| !v.is_empty())),
                )
            })
            .collect();
        println!("{}", Value::Object(status));
        return Ok(());
    }
    if args.live {
        return live(args, &quantity).await;
    }
    let data = MarketData {
        http: adapters::http()?,
        api_key: env::var("PREDICT_FUN_API_KEY")
            .context("Missing configuration: PREDICT_FUN_API_KEY")?,
    };
    let stop = if args.seconds == 0.0 {
        None
    } else {
        Some(Instant::now() + Duration::from_secs_f64(args.seconds))
    };
    loop {
        if stop.is_some_and(|stop| Instant::now() >= stop) {
            return Ok(());
        }
        let start = prediction_market::now_ms() / 1000 / 300 * 300;
        let markets = match data.discover(start).await {
            Ok(markets) => markets,
            Err(_) => {
                log::info!("AUTO_WAIT slot={start}");
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
        };
        let feed = Feeds::market(&markets, &data.api_key);
        let mut last_logged = None;
        loop {
            if stop.is_some_and(|stop| Instant::now() >= stop) {
                return Ok(());
            }
            if prediction_market::now_ms() / 1000 >= start + 300 {
                break;
            }
            let candidates = feed.candidates(&markets, &quantity).await?;
            if !candidates.is_empty()
                && last_logged.is_none_or(|at: Instant| {
                    at.elapsed() >= Duration::from_secs_f64(args.interval)
                })
            {
                for candidate in candidates {
                    println!(
                        "{}",
                        json!({"type":"CHECK","window":markets.window,"sides":candidate["sides"],"result":candidate["result"],"timestamps":candidate["timestamps"],"decision_at_ms":candidate["decision_at_ms"],"signal_to_decision_us":candidate["signal_to_decision_us"],"decision_compute_us":candidate["decision_compute_us"],"timing":candidate["timing"],"entry_reason":btc::entry_reason(&markets.window,&candidate["result"],[candidate["timestamps"][0].as_i64().context("Missing quote time")?,candidate["timestamps"][1].as_i64().context("Missing quote time")?],prediction_market::now_ms())?})
                    );
                }
                last_logged = Some(Instant::now());
            }
            let remaining = decision_timeout(start + 300, stop);
            tokio::select! {_=tokio::signal::ctrl_c()=>return Ok(()),updated=feed.wait_update(remaining)=>if !updated{break;}}
        }
    }
}

async fn live(args: BtcArgs, quantity: &d::Decimal) -> Result<()> {
    // Same journal path/OS lock as Python. Never clear or rotate it on startup.
    let mut journal = Journal::open(std::path::Path::new("data/prediction/live/orders.jsonl"))?;
    let mut clients = Clients::from_env()?;
    let _account_locks = AccountLocks::acquire(&[
        ("polymarket", clients.poly_maker),
        ("predict_fun", clients.predict_address()),
    ])?;
    clients.authenticate().await?;
    if !journal.waiting.is_empty() {
        for record in journal.waiting.clone() {
            let record = if record["type"] == "AWAIT_CONFIRMATION" {
                let markets = clients.data.load(record["window"].clone()).await?;
                let mut feed = Feeds::default();
                feed.watch_orders(
                    record["orders"]
                        .as_array()
                        .context("Missing recovery orders")?,
                )
                .await?;
                feed.users(&clients, &markets);
                trading::resume_confirmation(&mut clients, &mut journal, &record, &feed).await?
            } else {
                record
            };
            if record["type"] == "AWAIT_SETTLEMENT" {
                trading::wait_settlement(&clients, &mut journal, &record).await?;
            }
        }
        return Ok(());
    }
    let balances = clients.balances().await?;
    ensure!(
        balances.iter().all(|b| b >= &d::Decimal::from(15)),
        "Available balance below $15"
    );
    let stop = if args.seconds == 0.0 {
        None
    } else {
        Some(Instant::now() + Duration::from_secs_f64(args.seconds))
    };
    let mut attempts = 0;
    loop {
        if stop.is_some_and(|stop| Instant::now() >= stop) {
            return Ok(());
        }
        let start = prediction_market::now_ms() / 1000 / 300 * 300;
        let markets = match clients.data.discover(start).await {
            Ok(markets) => markets,
            Err(_) => {
                log::info!("AUTO_WAIT slot={start}");
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
        };
        if prediction_market::now_ms() / 1000 >= start + 300 {
            continue;
        }
        if args.preflight_only {
            clients
                .approvals(&markets, &[d::Decimal::from(15), d::Decimal::from(15)])
                .await?;
            if args.check_user_ws {
                let mut feed = Feeds::default();
                feed.users(&clients, &markets);
                let deadline = Instant::now() + Duration::from_secs(25);
                let mut stable = 0;
                while Instant::now() < deadline {
                    if feed.users_ready().await {
                        stable += 1;
                        if stable >= 3 {
                            println!("USER_WS_CHECK_OK no_orders_sent=true");
                            break;
                        }
                    } else {
                        stable = 0;
                    }
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
                ensure!(
                    stable >= 3,
                    "Account WebSocket did not receive a heartbeat/response"
                );
            }
            println!("LIVE_PREFLIGHT_OK no_orders_sent=true");
            return Ok(());
        }
        if journal.windows.contains(&start) {
            tokio::time::sleep(Duration::from_secs(5)).await;
            continue;
        }
        let mut feed = Feeds::market(&markets, &clients.data.api_key);
        feed.users(&clients, &markets);
        eprintln!(
            "AUTO_SWITCH slot={start} entry_deadline={} settlement_rules_require_independent_review=true",
            start + 240
        );
        loop {
            if stop.is_some_and(|stop| Instant::now() >= stop)
                || prediction_market::now_ms() / 1000 >= start + 240
            {
                break;
            }
            let mut submitted = None;
            for candidate in feed.candidates(&markets, quantity).await? {
                if let Some(record) = trading::execute(
                    &mut clients,
                    &mut journal,
                    &markets,
                    &candidate,
                    quantity,
                    &feed,
                    (attempts + 1, args.max_trades),
                )
                .await?
                {
                    submitted = Some(record);
                    break;
                }
            }
            if let Some(mut record) = submitted {
                attempts += 1;
                if record["type"] == "AWAIT_CONFIRMATION" {
                    record =
                        trading::resume_confirmation(&mut clients, &mut journal, &record, &feed)
                            .await?;
                }
                if record["type"] == "AWAIT_SETTLEMENT" {
                    trading::wait_settlement(&clients, &mut journal, &record).await?;
                    if record["stop_after"] == true {
                        return Ok(());
                    }
                }
                if attempts >= args.max_trades {
                    return Ok(());
                }
                break;
            }
            let remaining = decision_timeout(start + 240, stop);
            tokio::select! {_=tokio::signal::ctrl_c()=>return Ok(()),updated=feed.wait_update(remaining)=>if !updated{break;}}
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

fn decision_timeout(deadline_s: i64, stop: Option<Instant>) -> Duration {
    let window = Duration::from_millis(
        (deadline_s.saturating_mul(1000) - prediction_market::now_ms()).max(0) as u64,
    );
    stop.map(|stop| stop.saturating_duration_since(Instant::now()).min(window))
        .unwrap_or(window)
}
