# PaperBase

个人论文研究工作台：采集论文、按兴趣画像筛选、下载和解析 PDF、全文翻译，并提供带引用的论文问答。

## 当前能力

- PostgreSQL 元数据库，Docker Compose 一键启动；Markdown、PDF 使用持久化卷保存
- ACL Anthology、OpenAlex 期刊和 Crossref/NLE 采集器，支持增量抓取和断点状态
- 可扩展采集插件：内置采集器通过注册表统一调用，第三方可通过 `paperbase.sources` entry point 注册
- 可配置的兴趣画像：规则评分结合可选 LLM 复核，支持不同人的关键词、排除词和分类结果
- PDF 自动下载、MinerU 解析、标题/摘要及全文翻译，带哈希缓存和预算限制
- FastAPI 论文库、阅读队列、兴趣分类和研究早报界面；`paper` CLI 覆盖运维和批处理流程

## 快速开始

需要 Docker Engine；Compose v2 插件（`docker compose`）优先，也兼容旧版
`docker-compose`。仓库的 `ops/compose.sh` 会自动选择可用命令：

```bash
cp .env.example .env                 # 设置 POSTGRES_PASSWORD 和所需 API keys
cp config.example.toml config.toml   # 按需调整来源、兴趣画像和模型
chmod +x ops/compose.sh
./ops/compose.sh up -d --build
./ops/compose.sh exec web paper fetch  # 首次抓取论文；数据库会自动迁移
```

打开 <http://localhost:8000>。查看服务日志：

```bash
./ops/compose.sh logs -f web worker
```

生产环境可以把 `PAPERBASE_IMAGE` 设置为已发布的镜像，然后执行
`./ops/compose.sh pull && ./ops/compose.sh up -d`。若数据库密码包含 URL 保留字符，
请在 `.env` 中提供完整且已编码的 `DATABASE_URL`。

## 常用命令

```bash
docker compose exec web paper fetch --sources acl openalex crossref
docker compose exec web paper interest --profile research
docker compose exec web paper stats
docker compose exec web paper worker --once
docker compose exec web paper ask "这篇论文的方法是什么？" --paper <paper_id>
docker cp paper.pdf "$(docker compose ps -q web):/tmp/paper.pdf"
docker compose exec web paper upload /tmp/paper.pdf
```

`paper init --legacy-dir ...` 仅用于可选的旧 JSONL 数据迁移；新部署不需要导入 SQLite。

## 配置与目录

- `.env`：数据库密码、LLM/MinerU 密钥和部署变量；不要提交到 Git
- `config.toml`：来源、兴趣画像、翻译、存储、预算和访问控制配置
- `paperbase/`：应用源码；`paperbase/sources/`：采集器；`paperbase/interest/`：兴趣分类
- `tests/`：pytest 测试；`docs/` 和 `ops/`：需求、设计与部署文档

## 开发与测试

本地测试默认可使用 SQLite 临时数据库，不需要启动 PostgreSQL：

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

生产部署、镜像源、备份和更新步骤见 [`ops/deploy.md`](ops/deploy.md)。贡献规范见 [`AGENTS.md`](AGENTS.md)。
