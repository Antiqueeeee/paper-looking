# 多账号体系设计（阅读痕迹隔离方案）

当前版本是单用户系统。本文定义将来升级 TO C 多账号时，如何保证每个用户的
阅读状态、笔记、标签、问答历史等“阅读痕迹”完全隔离，同时不让论文处理和
解析工作重复。

## 1. 核心原则

| 数据 | 归属 | 说明 |
|---|---|---|
| papers 元数据 | 全局共享 | 标题、DOI、作者、期刊、年份、URL |
| pdf / parse / translate 状态 | 全局共享 | 一篇论文只需下载、解析、翻译一次 |
| 阅读状态 status | 用户私有 | new / in_queue / reading / done / later |
| 笔记 note | 用户私有 | |
| 用户标签 user_tags | 用户私有 | 系统标签 tags 仍是全局 |
| 问答历史 qa_logs | 默认共享 | 默认 public，对一篇论文的公共提问/回答全员可见；可加 private 选项 |
| 阅读时长/进度 | 用户私有 | 打开时间、停留时长等 |

一句话：**论文事实和加工状态全局一份；人和论文之间的关系每人一份。**

## 2. 目标表结构

```sql
CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    expires_at TEXT NOT NULL
);

-- 每个用户对每篇论文的个人状态
CREATE TABLE user_paper_state (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    paper_id   TEXT NOT NULL REFERENCES papers(id),
    status     TEXT NOT NULL DEFAULT 'new',
    note       TEXT NOT NULL DEFAULT '',
    user_tags  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, paper_id)
);

CREATE INDEX idx_user_state_status
    ON user_paper_state(account_id, status, updated_at);

-- 阅读会话（可统计“看过多久”）
CREATE TABLE reading_sessions (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    paper_id   TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    seconds    INTEGER
);

-- 问答历史：默认共享，可追溯提问者；支持"仅自己可见"
ALTER TABLE qa_logs ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';
ALTER TABLE qa_logs ADD COLUMN account_id INTEGER;
```

`papers.status / note / user_tags` 三列在多账号上线后废弃，数据一次性迁入
`user_paper_state`。

## 3. 现有数据迁移

给单用户时代的默认账号 `id=1` 做一次迁移：

```sql
INSERT INTO accounts(id, email, password_hash, created_at)
VALUES (1, 'owner@local', '', datetime('now'));

INSERT INTO user_paper_state(account_id, paper_id, status, note, user_tags, updated_at)
SELECT 1, id, status, note, user_tags, updated_at FROM papers;
```

之后代码不再直接读写 `papers.status / note / user_tags`。

## 4. API 隔离改造

```text
登录前：
  POST /api/auth/login
  POST /api/auth/register

登录后（Header: Authorization: Bearer <token>）：
  GET  /api/me/papers              用户视角论文列表（join user_paper_state）
  GET  /api/me/queue               用户的阅读队列
  PATCH /api/me/papers/{id}        更新自己的状态/笔记/标签
  GET  /api/me/stats               自己的阅读统计
  POST /api/me/ask                 问答（写入 qa_logs.account_id）
```

未登录或不存在的 paper 直接 404；所有查询强制带 `account_id`，禁止跨用户
读取 `user_paper_state`。

## 5. DCI 问答的共享与隔离

- 论文全文 `md/` 语料目录对登录用户仍可检索（论文本身公共）。
- 问答默认 `visibility='public'`：后来者打开论文时能看到已有问答，避免重复提问和重复消耗 API。
- 用户提问时可选 `visibility='private'`，只写入当前 `account_id`，其他用户不可见。
- DCI 的 `sqlite_query` 不直接暴露 `accounts / private 问答 / user_paper_state`；
  公共问答通过只读视图提供。

## 6. 对当前代码的影响评估

| 模块 | 改动 |
|---|---|
| 采集 / 去重 / 入库 | 不改 |
| PDF 下载 / MinerU / 全文翻译 | 不改，仍是全局任务 |
| 规则标签 tags | 不改，仍是全局 |
| 早报生成 | 基本不改；可增加“只看我未读的”个性化视图 |
| Web/CLI 状态读写 | 改为通过 user_paper_state + 当前用户 |
| DCI | 增加视图权限，qa_logs 加 account_id |
| 部署 | 增加 auth 服务或中间件；SQLite 仍可支撑早期规模 |

## 7. 何时需要拆

单用户阶段继续保持当前简单结构。出现以下任一信号时执行本文方案：

1. 出现第二个用户；
2. 需要“我读过的 / 我没读过的”与设备无关地跨端同步；
3. 需要分享库但隐藏个人笔记和阅读记录。
