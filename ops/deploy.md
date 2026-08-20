# PaperBase Docker Deployment

This deployment consists of PostgreSQL (`db`), the web application (`web`),
and the background worker (`worker`). Named Docker volumes retain database,
PDF, and Markdown data across container recreation.

## 1. Install Docker and Compose

Install Docker Engine from the official Docker repository. The Compose v2
plugin (`docker compose`) is preferred, but the project also supports the
legacy standalone binary (`docker-compose`). All commands below use the
repository wrapper, which selects whichever is installed:

```bash
./ops/compose.sh version
```

If neither command is available, install the Compose plugin before continuing.

## 2. Configure Docker Registry Mirrors

The current development machine uses these Docker Hub mirrors, recorded in
[`ops/docker-daemon.json.example`](docker-daemon.json.example):

```json
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
```

For a server without `/etc/docker/daemon.json`, install that file, then restart
Docker. Restarting Docker briefly stops running containers.

```bash
sudo install -d -m 0755 /etc/docker
sudo install -m 0644 ops/docker-daemon.json.example /etc/docker/daemon.json
sudo systemctl restart docker
docker info --format '{{json .RegistryConfig.Mirrors}}'
```

If `/etc/docker/daemon.json` already exists, back it up and merge only the
`registry-mirrors` field into its existing JSON before restarting Docker. Do
not replace unrelated daemon settings. Use only mirrors trusted by the server
operator; a mirror is infrastructure outside this repository.

## 3. Configure and Start

```bash
sudo mkdir -p /opt/paper && sudo chown "$USER" /opt/paper
cd /opt/paper
git clone git@github.com:Antiqueeeee/paper-looking.git .
cp .env.example .env
cp config.example.toml config.toml
chmod +x ops/compose.sh
```

Edit `.env`: set a strong `POSTGRES_PASSWORD` and required API keys. Compose
derives `DATABASE_URL` from those PostgreSQL variables. If the password has
`@`, `:`, `/`, or another URL-reserved character, set an explicit URL-encoded
`DATABASE_URL` instead.

Build and start locally on the server:

```bash
./ops/compose.sh up -d --build
./ops/compose.sh ps
./ops/compose.sh logs -f web worker
curl --fail http://127.0.0.1:8000/api/health
./ops/compose.sh exec web paper fetch
```

The first start applies PostgreSQL migrations automatically. Do not create a
separate SQLite database. Keep port 8000 private behind Tailscale or a reverse
proxy when exposing the web UI.

## 4. Update and Operate

For the local Dockerfile build:

```bash
git pull --ff-only
./ops/compose.sh up -d --build
./ops/compose.sh ps
```

For a published application image, set `PAPERBASE_IMAGE=registry.example/paperbase:tag`
in `.env`, then run:

```bash
./ops/compose.sh pull
./ops/compose.sh up -d
```

Inspect failures with `./ops/compose.sh logs --tail=200 web worker`. Use
`./ops/compose.sh down` only for service shutdown: named volumes remain. Do
not add `-v` unless intentionally deleting all database and paper data.

## 5. Back Up and Restore

Back up PostgreSQL before an update and separately preserve the `paper_data`
and `paper_files` volumes containing generated Markdown and PDFs:

```bash
mkdir -p /backup/paperbase
set -a; . ./.env; set +a
./ops/compose.sh exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  > /backup/paperbase/paperbase-$(date +%F).sql
```

Restore into an empty database with `psql`; verify the dump can be read before
relying on it as a backup.
