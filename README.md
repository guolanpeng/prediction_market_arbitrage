# 预测市场跨平台扫描与配对交易

独立于原套利应用的预测市场项目，位于
`D:\WorkSpace\prediction_market_arbitrage`。保留 `user_strategies` 模块入口，
包含市场目录采集、Synpath 扫描结果导入、Redis 快照、BTC 五分钟盘口检查、
模拟记录，以及 Polymarket / Predict.fun 实盘与 WS＋REST 成交核对。

## 安装

从本仓库根目录运行，需要 uv 与 Python 3.13：

```powershell
cd D:\WorkSpace\prediction_market_arbitrage
uv sync --project python --locked --group test --no-build
Copy-Item .env.example .env
```

填写本地 `.env` 中的六项：Polymarket 私钥、资金钱包地址、签名类型，以及
Predict.fun API Key、签名私钥、资金账户地址。模板按目前使用的 Deposit Wallet
设置 `POLYMARKET_SIGNATURE_TYPE=3`；换钱包类型时需要重新核对。模板中没有实际密钥。
本项目不依赖交易引擎，不需要 `engine-wheels`。

可选配置不必填写：Polymarket 的 API Key / Secret / Passphrase 由 SDK 自动派生；
RPC 使用默认主网地址。需要自定义时，可额外添加 `POLYMARKET_RPC_URL` 或
`PREDICT_FUN_RPC_URL`。Redis 默认连接 `redis://127.0.0.1:6379/0`，需要修改时添加
`MARKET_REDIS_URL`。五平台扫描如需 Opinion，可另外添加 `OPINION_API_KEY`。

## 只读检查

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --quantity 10
```

模拟记录：

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --paper --quantity 10 --records data/prediction/paper/btc.jsonl
```

读取当前资金和授权，不下单：

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1 --preflight-only
```

## Redis 扫描

需要自行运行 Redis；下面两个采集方式选择一个运行，避免互相覆盖快照。

```powershell
# Synpath 公共扫描结果，默认方式
uv run --project python --no-sync --env-file .env python -m user_strategies.prediction_scan --source synpath

# 各平台主网目录和匹配后的盘口
uv run --project python --no-sync --env-file .env python -m user_strategies.prediction_scan --source venues
```

Redis 的 `prediction:v1` 键和 JSON 字段保持不变，现有控制面板可继续读取。
完整市场目录写入本地 `data/prediction/catalogs`。缺失平台 API Key 时明确显示不可用。

## 切换旧项目的实盘状态

本仓库迁移的是源代码、文档、测试及依赖，不含旧 `.env`、日志、市场目录或交易日记账。
原项目 `D:\WorkSpace\arbitrage_app` 中的文件仍保留。

切换前先停止旧实盘进程，核对旧 `data/prediction/live/orders.jsonl` 是否有未决订单。
将旧日记账作为私有运行数据转移到本仓库的同一路径后再启动实盘；不能以空日记账
绕过旧订单的核对状态。只有一个实盘进程可以使用同一账户，避免两个仓库重复下单。
`.env` 与日记账由用户自行迁移，均被 Git 忽略。

完成上述切换后，这条命令会真实下单：

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1
```

当前门槛是预计扣费、扣份与缓冲后的每组差价 > $1，两边可用余额 ≥ $15，
最后 60 秒不新开仓，同一窗口最多尝试一次。Polymarket 按现金预算买入，
实际份数会变化；Predict.fun 使用目标份数与预算约束。两边超过 0.5 份差额时
尝试退出多余部分，再等待正式结算。当前退出仍是一次 IOC 尝试，未增加分批持续退出。
两边 BTC 结算依据不同，可能两边都输，不能将报价差视为保证收益。

两边账户 WS 提供订单/成交推送，REST 继续核对。确认超时转为只读继续查询，
不自动重复下单。退出金额计算保留 `reduce_only`，避免将 YES 卖价按 NO 方向反转。

## 测试与依赖

```powershell
uv run --project python --no-sync python -m pytest user_strategies -q
```

`python/uv.lock` 锁定全部依赖；`python/requirements.txt` 是带哈希的运行依赖导出。
离线安装需预先准备与平台匹配的 wheels，安装时启用 `--require-hashes`。
本仓库不包含 wheels、虚拟环境或密钥作为版本控制内容。

详细说明：

- [BTC 检查与模拟](user_strategies/CHECK_PREDICTION_PAIR.md)
- [Redis 扫描](user_strategies/PREDICTION_SCAN.md)
- [实盘与核对](user_strategies/PREDICTION_TRADING.md)
