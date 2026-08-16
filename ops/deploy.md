# PaperBase 部署手册（2C4G / 20GB VPS）

## 1. 系统准备

```bash
sudo useradd --create-home --system paper
sudo mkdir -p /opt/paper
sudo chown paper:paper /opt/paper
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip ripgrep curl unzip
```

## 2. 安装应用

```bash
cd /opt/paper
python3 -m venv venv
./venv/bin/pip install -e .            # 或直接拷贝 paperbase 目录
cp config.example.toml config.toml     # 填写 LLM / MinerU / 对象存储
```

设置密钥（推荐直接放在 `/opt/paper/.env`，应用启动时自动加载）：

```bash
sudo -u paper tee /opt/paper/.env >/dev/null <<'ENV'
DEEPSEEK_API_KEY=...
MINERU_API_KEY=...
PAPERBASE_CONFIG=/opt/paper/config.toml
ENV
sudo chmod 600 /opt/paper/.env
```

也可以使用 systemd 的 `EnvironmentFile=/etc/paper.env`，两种方式二选一。

## 3. systemd 服务（按需运行，无常驻 worker）

```bash
sudo cp ops/paper-web.service ops/paper-worker.service ops/paper-worker.timer /etc/systemd/system/
# 若使用 /etc/paper.env 方式，在 [Service] 段加入：
# EnvironmentFile=/etc/paper.env
# 若使用 /opt/paper/.env，则无需修改 service 文件。
sudo systemctl daemon-reload
sudo systemctl enable --now paper-web          # Web 常驻（访问页面需要）
sudo systemctl enable --now paper-worker.timer # 每周一 02:00 一次性抓取
```

- 在页面点“想读”或上传 PDF 后，Web 服务会**按需在后台启动任务处理**，跑完即停。
- 手动处理积压任务：`./venv/bin/python -m paperbase.cli worker`
- 不需要再运行 `worker --loop` 常驻进程。

访问方式：服务只监听 `127.0.0.1:8000`，不对公网开端口。

### 推荐：Tailscale（个人首选）

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# 手机/电脑安装 Tailscale 后访问：
# http://<tailscale-ip>:8000
```

优点：无公网端口、无域名/备案要求、端到端加密、各端体验一致。

### 备选：Cloudflare Tunnel（浏览器直接访问）

```bash
# 需要你有一个域名托管在 Cloudflare
cloudflared tunnel login
cloudflared tunnel create paperbase
cloudflared tunnel route dns paperbase paper.example.com
cloudflared tunnel run --url http://127.0.0.1:8000 paperbase
```

优点：手机浏览器直接打开域名，无需安装客户端；大陆访问速度需实测。

### 不建议

- 不建议直接对公网开放 `8000` 端口；
- 阿里云大陆 ECS 使用 80/443 域名访问通常涉及 ICP 备案，Tailscale/Tunnel 可绕过这个流程。

## 4. 初始化与验收

```bash
cd /opt/paper
./venv/bin/python -m paperbase.cli init --legacy-dir /path/to/legacy
./venv/bin/python -m paperbase.cli today --no-translate
./venv/bin/python -m paperbase.cli upload paper.pdf
./venv/bin/python -m paperbase.cli worker --once
curl http://127.0.0.1:8000/api/health
```

## 5. 备份

安装 Litestream 后复制 `ops/litestream.yml`，或至少每日执行：

```bash
sqlite3 /opt/paper/data/papers.db ".backup /backup/papers.db"
```

PDF 冷数据由应用自动上传对象存储；Markdown 目录建议每周 `rsync` 到对象存储/另一台机器。

## 6. 磁盘策略

- 本地热 PDF 配额默认 6GB，达到后解析完成的 PDF 自动转冷。
- 磁盘使用率 80% 告警，90% 暂停下载和全文翻译任务。
- 20GB 磁盘下 Markdown 全文约 1 万篇占 1.5GB，无需额外处理。
