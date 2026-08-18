# PaperBase Docker 部署手册

## 准备

在 VPS 安装 Docker Engine 与 Compose 插件，然后克隆项目：

```bash
sudo mkdir -p /opt/paper && sudo chown "$USER" /opt/paper
cd /opt/paper
git clone <repository-url> .
cp .env.example .env
cp config.example.toml config.toml
```

编辑 `.env`，至少设置高强度的 `POSTGRES_PASSWORD`，并按需填写
`DEEPSEEK_API_KEY`、`MINERU_API_KEY`。数据库默认使用 Compose 内的 `db`
服务。密码含 `@`、`:` 等 URL 保留字符时，在 `.env` 中设置完整、URL 编码后的
`DATABASE_URL`。

## 启动与验收

```bash
docker compose up -d --build
docker compose logs -f web worker
curl http://127.0.0.1:8000/api/health
docker compose exec web paper fetch
```

首次启动会自动执行 PostgreSQL schema migration。Web 和 worker 共享数据库、
Markdown 与 PDF 卷；不要分别手动创建数据库文件。推荐使用 Tailscale 或反向代理
暴露 Web 服务，而不是直接公开 8000 端口。

## 更新

本地构建部署：

```bash
git pull
docker compose up -d --build
```

已发布镜像部署：在 `.env` 设置 `PAPERBASE_IMAGE=registry.example/paperbase:tag`，然后：

```bash
docker compose pull
docker compose up -d
```

## 备份

每天备份 PostgreSQL，并另行备份 Docker 卷中的 Markdown/PDF：

```bash
set -a; . ./.env; set +a
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > /backup/paperbase.sql
```

恢复时使用 `psql` 导入该文件到空数据库。更新镜像前先验证备份可读。
