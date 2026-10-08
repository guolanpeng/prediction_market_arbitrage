# Ubuntu 24.04 x86_64 迁移

本页原流程为 Python 部署。Rust 版本可在本机 WSL Ubuntu 22.04 编译完成后上传
已构建的 Linux 程序，服务器不需要 Rust 编译器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\upload_rust.ps1 -BuildOnly
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\upload_rust.ps1
```

第二条会上传和安装，不启动实盘。详细配置、22.04/24.04 兼容性检查与数据保护
见 [Rust BTC 上传说明](RUST_BTC.md)。

从项目根目录运行。使用 uv 管理的 `python/.venv`，Python 3.13；不需要
NautilusTrader、Rust 编译工具或交易引擎。依赖支持 Windows 和 Linux，版本由
`python/uv.lock` 锁定。发布包同时包含 uv 导出的带哈希 `python/requirements.txt`。

## 上传代码与安装

Windows 本机可直接上传并安装，不必手动打包。
将 `deploy.example.json` 复制为 `deploy.local.json`，填写 SSH 用户名、host、port 和 remote_directory，
然后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\upload_deploy.ps1
```

`deploy.local.json` 被 Git 忽略，也不会上传。命令行 `-Server`、`-RemoteDirectory`、`-Port`
可覆盖配置中的对应值，`-ConfigFile` 可指定另一份本地配置。
脚本使用当前工作区代码（包括尚未提交的修改），
生成不含密钥和运行数据的归档，通过 scp 上传，再通过 SSH 调用 Ubuntu 安装脚本。
本地需要 Windows OpenSSH Client 和 tar。首次连接确认服务器 SSH 指纹，随后按 SSH 提示认证；
已有 curl 和 flock 时，安装完全在用户目录完成，不需要 sudo。
缺少必要系统工具时，须由管理员安装；脚本不会等待 sudo 密码。
安装失败时立即停止，保留远端暂存包以便排查。
更新已运行的服务器时，先停止实盘；锁被占用时拒绝解压代码和更新环境。
安装不会迁移本地账户数据，实盘切换仍需完成下文的私有数据迁移和只读预检。

本地 PowerShell 将代码发布包上传到自己的服务器，替换 `USER@SERVER`：

```powershell
scp .\dist\prediction-market-arbitrage-ubuntu24-x86_64.tar.gz USER@SERVER:~/
```

服务器上执行：

```bash
tar -xzf ~/prediction-market-arbitrage-ubuntu24-x86_64.tar.gz -C ~/
cd ~/prediction_market_arbitrage
bash scripts/deploy_ubuntu.sh
```

uv 安装方式见 [官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)。
脚本会安装 uv、创建 Linux Python 环境、安装锁定依赖、运行测试，
并只检查凭证是否填写。不启动实盘、不发送授权或赎回交易。缺失 `.env` 时创建空模板，
已有 `.env` 和交易日记账不覆盖；检测到当前目录的日记账锁被占用时拒绝部署。
不要在任何机器仍运行该账户的实盘时更新环境或切换账户进程。
curl 和 flock 是必要工具；缺少时由管理员安装 `curl ca-certificates util-linux`。
tmux 仅用于保持会话，不是部署依赖，缺少它不会触发 sudo 或阻止安装。
不要复制 Windows 的 `.venv`。服务器安装失败时保留锁文件，先排查失败原因，
不要通过删锁文件、升级交易 SDK 或允许源码编译来绕过。

## 实盘切换：先停止本地，再复制私有数据

确认本地实盘已完全退出，并保留所有未决交易记录。完成结算/核对后再切换，
不要在存在未知订单时开启另一台机器的交易进程。

服务器先创建私有数据目录：

```bash
cd ~/prediction_market_arbitrage
mkdir -p data logs
chmod 700 data logs
```

本地项目根目录执行以下 PowerShell 命令。`.env` 单独通过 SSH 传输，不在代码发布包内：

```powershell
scp .\.env USER@SERVER:~/prediction_market_arbitrage/.env
scp -r .\data\prediction USER@SERVER:~/prediction_market_arbitrage/data/
scp -r .\logs\* USER@SERVER:~/prediction_market_arbitrage/logs/
```

目标必须是新安装目录，不要覆盖服务器上正在使用的日记账。完整运行数据包括
`data/prediction/live/orders.jsonl`、核对证据和模拟记录；日志用于历史分析。
复制过来的 `.lock` 文件不携带操作系统锁，不能防止本地与服务器同时交易。
必须保证同一账户只在一台机器运行实盘。原文件保留作为备份。

服务器限制私有文件权限，然后做只读检查：

```bash
cd ~/prediction_market_arbitrage
chmod 600 .env
chmod -R go-rwx data logs
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --trade-credentials
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1 --preflight-only
```

账户和授权检查通过后才能启动实盘。未决/HALT 日记账仍需核对，不要清空它。
服务器也需要能访问两个平台、WebSocket 和相应链的 RPC；安装成功不证明交易网络可用。
仅 BTC 检查和实盘不需要 Redis；`prediction_scan` 才需要 Redis。

## 保持终端运行

先创建 tmux 会话，进入后在其中运行实盘：

以下方式需要 tmux 已安装；未安装时可先保持 SSH 前台运行，或请管理员安装 tmux。

```bash
tmux new -s prediction
cd ~/prediction_market_arbitrage
export PATH="$HOME/.local/bin:$PATH"
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1
```

最后一条命令会真实下单。`Ctrl+B` 后按 `D` 脱离会话，SSH 断开时程序继续运行；
用 `tmux attach -t prediction` 回到会话。同一账户只启动一个实盘会话。
`--max-trades 1` 完成一次尝试和后续跟踪后会退出，不配置自动重启来循环下单。

日志在本仓库 `logs/prediction/live-YYYY-MM-DD.log`，日期和时间按服务器时区。
服务器如使用 UTC，与本地北京时间相差 8 小时；市场窗口本身以 UTC 时间戳计算。

```bash
tail -F logs/prediction/live-*.log
```

日志按启动日期选文件，跨午夜仍追加原文件，下一次启动才使用新日期。

## 离线安装

先在相同 Python 版本、CPU 架构和 Linux ABI 的联网机器准备带锁定版本的 wheels，
单独传输。不要将 wheels 或运行凭证加入 Git，也不要将 Windows wheels 用于 Linux。

```bash
cd ~/prediction_market_arbitrage
uv venv python/.venv --python 3.13
uv pip install --python python/.venv/bin/python --no-index --find-links /path/to/wheels --require-hashes --only-binary :all: -r python/requirements.txt
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --trade-credentials
```

离线使用 `--no-sync`，不让 uv 在运行时尝试重新下载依赖。缺少 wheel 时先补齐，
不禁用哈希校验。离线 Python 解释器也需要预先准备。
