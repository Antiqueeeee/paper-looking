# PaperBase

个人论文库：定时获取论文、兴趣筛选、中文早报、PDF 解析、全文翻译和 DCI 全库问答。

## 特性

- 多源采集：ACL Anthology / OpenAlex 期刊 / arXiv（可选），增量可续跑
- 兴趣规则匹配（kg / ie / kbqa / rag / mem 等）与每日中文早报
- 开放 PDF 自动下载；未开放 PDF 手动上传，SHA-256 去重
- MinerU API 解析 PDF 为 Markdown，无需本地 GPU
- 标题摘要 + 全文翻译，带哈希缓存和每日预算熔断
- DCI 问答：`rg` + 只读 SQLite，不依赖向量数据库，回答带 `文件:行号` 引用
- PostgreSQL 元数据库（Docker Compose），Markdown 文件作为语料，PDF 冷热分离
- FastAPI Web 界面 + `paper` CLI

## 快速开始

```bash
cp .env.example .env                 # 设置 POSTGRES_PASSWORD 和 API keys
cp config.example.toml config.toml
docker compose up -d --build
docker compose exec web paper fetch   # 重新抓取论文；首次启动会自动迁移 PostgreSQL
docker compose logs -f web worker
```

访问 `http://localhost:8000`。生产部署使用 `docker compose pull && docker compose up -d`，并设置 `PAPERBASE_IMAGE` 为已发布的镜像。若密码含 URL 保留字符，可在 `.env` 设置完整且已编码的 `DATABASE_URL`。

## 配置

复制 `config.example.toml` 为 `config.toml`，或设置 `PAPERBASE_CONFIG` 环境变量。

- 推荐把密钥放在项目根目录 `.env` 文件（见 `.env.example`），启动时自动加载；也可以继续用环境变量
- LLM 默认 DeepSeek：`.env` 中填 `DEEPSEEK_API_KEY`；切换 provider 只改 `[llm] provider`
- MinerU：`.env` 中填 `MINERU_API_KEY`（只从环境变量/`.env` 读取）
- 对象存储：默认本地 `filesystem`，后续可切 `oss` / `s3`
- 每日预算、磁盘阈值均在配置中

## 测试

```bash
python -m pytest
```

## 部署

见 [`ops/deploy.md`](ops/deploy.md)。目标配置：2C4G / 20GB VPS，SQLite + 文件系统，Tailscale 访问。

## 文档

- 需求与验收：`docs/BDD.md`
- 并行任务计划：`docs/IMPLEMENTATION_PLAN.md`
