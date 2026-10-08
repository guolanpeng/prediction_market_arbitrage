# BTC 五分钟实盘 Rust 迁移

Rust 程序只处理 Polymarket / Predict.fun 的 BTC 五分钟配对链路。市场扫描、Redis
`prediction:v1` 契约和其他 Python 策略没有迁移或改变。原 Python 实盘实现仍保留，
默认入口继续使用 Python；`--backend rust` 显式选择 Rust。

## SDK 和金额

Polymarket 使用官方 `polymarket_client_sdk_v2 = 0.8.0`，负责认证、订单签名、
提交、余额以及订单/成交查询。保留 CLOB V2 订单和签名类型 0–3，包括 Deposit Wallet。
按现金预算构造 FAK BUY，`makerAmount` 是资金，`takerAmount` 是保护价下的最低份数。
不使用 SDK 自动按报价调整现金量的路径，避免改变既有预算。

Predict.fun 当前官方提供 Python/TypeScript SDK。按用户选择，运行时保持纯 Rust：
原生 EIP-712、Kernel Smart Wallet 包装和登录签名；官方 Python `predict-sdk==0.0.22`
只用于离线对照测试。测试覆盖四种交易合约、YES/NO、EOA/Smart Wallet，比较完整
签名字节和订单哈希；目标份数与 1% 滑点资金预算也与官方 SDK 对照。

业务计算使用 `bigdecimal` 和显式 64 位有效数字、half-even 上下文；费用按原规则
half-up 舍入。链上资金、份数和签名字段使用 `U256`，不经过二进制浮点数。
Predict.fun 份数截取五位有效数字，价格/现金以 18 位最小单位计算。

## 构建

从仓库根目录执行，需要 Rust 1.98.1 和平台 C/C++ 构建工具。只编译本独立程序
及其库依赖，不依赖或编译 NautilusTrader。

```powershell
cargo build --release --locked
```

Release 性能配置为 `opt-level=3`、Fat LTO、`codegen-units=1`，关闭增量编译与
调试信息。保留整数溢出检查和异常展开，不改变价格精度、风控及日记账恢复。
使用默认通用 CPU 指令集，不启用 `target-cpu=native`。Windows 构建脚本：

```powershell
.\scripts\build_rust.ps1 -Offline
```

脚本默认用 4 个编译任务以控制构建内存；`-Jobs` 只影响编译速度，不影响程序运行
优化等级。构建不绑定本机 CPU，使用当前平台的默认指令集；Windows 二进制不能
直接用于 Linux，目标平台仍需分别构建。构建不会启动交易程序。

`Cargo.lock` 锁定 Rust 依赖及 registry 包校验和。`target/`、二进制和运行数据被 Git
忽略。Python `python/uv.lock` 和带哈希的导出 requirements 保持原样。
离线构建需预先准备已锁定的 Cargo 依赖，使用 `cargo build --release --locked --offline`。
现有 Python 发布、安装和部署脚本仍只处理 Python，不会自动发布 Rust 二进制。
没有制作发布包，没有在仓库中包含凭证。

## 运行

以下只检查配置是否存在，不连接平台：

```powershell
.\target\release\prediction-market.exe btc --trade-credentials
```

行情检查只需要 Predict.fun API Key，自动连接当前 BTC 窗口的两边行情 WS：

```powershell
.\target\release\prediction-market.exe btc --quantity 10 --seconds 30
```

读取账户余额与已有授权，不授予授权、不下单：

```powershell
.\target\release\prediction-market.exe btc --live --preflight-only --quantity 10
```

同时检查账户 WebSocket 连接及心跳/订阅响应，仍不下单：

```powershell
.\target\release\prediction-market.exe btc --live --preflight-only --check-user-ws --quantity 10
```

**以下命令会真实下单；迁移开发与测试没有执行它。**

```powershell
.\target\release\prediction-market.exe btc --live --quantity 10 --max-trades 1
```

也可保留现有模块路径，显式选择 Rust：

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --backend rust --auto-btc --live --quantity 10 --max-trades 1
```

Rust 读取同一份 `.env`，也可使用进程已有环境变量；环境中已存在的值优先。
禁止测试网模式。Python 薄入口只启动本机编译后的 Rust 程序；Rust 运行时不会
调用 Python 或 Node.js。Rust 入口不支持市场扫描、固定市场 ID 或 paper 模式。

## 执行与恢复

- 最后 60 秒不开仓；目标份数不超过 10；同一窗口最多尝试一次。
- Rust 开仓判断由已验证盘口更新触发，任意一边更新即可唤醒计算，不等待固定的
  2 秒轮询。密集更新合并为读取最新状态，避免排队处理过时的判断。
  `--interval` 仅限制 CHECK 日志输出频率，不限制策略计算。无更新时等待事件或
  窗口/运行截止时间；原 Python 策略的固定间隔判断保持原样。
- 报价不得超过 15 秒，两个时间戳相差不得超过 5 秒；Polymarket 盘口增量合并后
  检查平台最优价格，失配或消息时间倒序时只失效并重新订阅受影响的盘口。
  持续更新期间只暴露上一份已通过校验的快照，未校验的增量不用于交易；断线
  会使可执行盘口失效。
- 初始净价差和考虑 Predict 扣份、预算、费用及缓冲后的净收益均须严格超过 $1。
- 两边可用余额至少 $15，且足以覆盖预算与费用保留额；两边买入授权和份数卖出
  操作权限必须已经存在。程序没有授予授权、转账或自动赎回功能。
- 两边订单先持久化 `INTENT` 和 `CASH_SUBMIT_INTENT`，再并发各提交一次。
  不重试交易 POST；返回未知、超时或成交未确认时只继续查询，不重复创建订单。
- REST 和账户 WS 成交记录按身份合并；终态不得回退，金额/费用冲突停止确认。
  余额变化不能作为成交证明。手续费扣份计入可卖库存。
- 超过 0.5 份差额时最多尝试一次卖出多余部分；保护价来自有效卖盘，明确减少
  既有份数。退出确认未知会写入 HALT；延迟确认后不自动补单或退出。
- 两边正式结算后记录实际现金流；费用未知时不输出已验证净收益。两边 BTC
  结算依据可能不同，保留两边都输的情况，不把报价差当作保证收益。

使用现有 `data/prediction/live/orders.jsonl`，append + fsync，不清空旧记录。
已有 `AWAIT_CONFIRMATION` / `AWAIT_SETTLEMENT` 会先恢复只读核对；未决 INTENT、
HALT 或截断记录阻止新开仓。Rust 和 Python 使用同一个 journal 锁，还共享本机
按资金账户区分的 OS 文件锁，防止不同 journal 路径同时使用同一账户。
锁不能协调不同主机；账户迁移时必须停止原主机上的实盘进程。

日志追加到 `logs/prediction/check-YYYY-MM-DD.log` 或 `live-YYYY-MM-DD.log`。
不记录私钥、API Secret、JWT、签名和平台原始错误体。

## 离线验证

```powershell
cargo test --locked
cargo clippy --all-targets --locked -- -D warnings
cargo build --locked
uv run --project python --no-sync python -m pytest user_strategies -q
```

Python 对照测试默认使用 `target/debug/prediction-market`（Windows 加 `.exe`）。
可设置 `PREDICTION_RUST_TEST_BINARY` 为已构建程序的绝对路径。
未构建 Rust 时这些对照测试显示 skipped；迁移验收应构建程序并确认对照测试确实运行。
所有签名测试使用公开的虚构测试私钥；不读取 `.env`，不访问真实钱包。
离线测试不等同于实盘环境验证。已用本机配置通过账户认证、余额、已有授权、
账户 WebSocket 和 BTC 行情 WebSocket 的只读联调；没有下单、新增授权或部署。
真实成交及成交推送尚未通过实单验证。

CHECK 的 `decision_at_ms` 和 `timing` 记录本地接收时间、收包到计算完成、
盘口校验等待和校验完成到计算完成的耗时（微秒）。这些字段测量客户端内部耗时，
不能替代网络 RTT；`source_age_ms` 还受到平台报价更新频率和时钟差异影响。
`signal_to_decision_us` 单独记录最新盘口更新通知到计算完成的耗时，
`decision_compute_us` 记录本轮计算耗时；用于区分事件调度与报价本身的新旧。
