# 个人论文库实现计划与并行任务分配

- 版本：v0.1
- 验收依据：`docs/BDD.md`
- 部署目标：2C4G / 20GB VPS，SQLite(WAL) + 文件系统 + MinerU API + LLM API + 对象存储
- 工作方式：先冻结 Wave 0 契约层，之后各 Agent 按任务卡并行开发，最后统一集成。

---

## 1. 目标架构

```text
                    ┌──────────────────────────────────────────┐
                    │            CLI + Web (Agent E)           │
                    │   paper today / ask / read / upload      │
                    └───────┬──────────────────────┬───────────┘
                            │                      │
              ┌─────────────▼─────────┐  ┌─────────▼──────────────┐
              │ 数据采集 Agent A       │  │ DCI 问答 Agent D        │
              │ sources/ acl/openalex │  │ dci/tools,agent,prompts │
              │ arxiv + init 导入      │  │ rg/sed/sqlite 只读工具  │
              └─────────────┬─────────┘  └─────────┬──────────────┘
                            │                      │
              ┌─────────────▼──────────────────────▼──────────────┐
              │        Wave 0 共享契约：db / paths / config /      │
              │        models / tasks / llm protocol / storage    │
              └─────────────┬──────────────────────┬──────────────┘
                            │                      │
              ┌─────────────▼─────────┐  ┌─────────▼──────────────┐
              │ 筛选/翻译/早报 Agent B │  │ PDF/MinerU Agent C      │
              │ pipeline/filter,       │  │ pipeline/pdf,mineru,   │
              │ translate,digest       │  │ storage(lru/cos)       │
              └───────────────────────┘  └────────────────────────┘
```

数据流（只允许通过 SQLite + 文件系统 + 契约接口）：

```text
sources → papers 表 → 兴趣规则 → title_zh/abstract_zh → digest
                    ↘ in_queue 论文
                         → download_pdf → MinerU API → md 文件
                         → translate_full → zh.md 文件
                         → DCI Agent 只读搜索 → 带引用回答
```

---

## 2. 仓库目标结构

```text
paper-looking/
├── docs/BDD.md
├── docs/IMPLEMENTATION_PLAN.md
├── pyproject.toml
├── config.example.toml
├── paperbase/
│   ├── __init__.py
│   ├── config.py          # Wave 0：配置加载
│   ├── paths.py           # Wave 0：唯一路径规则
│   ├── db.py              # Wave 0：SQLite schema 与连接
│   ├── models.py          # Wave 0：枚举、dataclass、协议
│   ├── tasks.py           # Wave 0：任务状态机
│   ├── llm.py             # Wave 0：LLM 协议 + 计费日志占位
│   ├── storage.py         # Wave 0：对象存储协议 + 本地磁盘配额接口
│   ├── sources/           # Agent A
│   │   ├── __init__.py
│   │   ├── acl.py
│   │   ├── openalex.py
│   │   ├── arxiv.py
│   │   └── import_legacy.py
│   ├── pipeline/          # Agent B + C
│   │   ├── __init__.py
│   │   ├── filter.py      # Agent B
│   │   ├── translate.py   # Agent B + C 共用
│   │   ├── digest.py      # Agent B
│   │   ├── pdf.py         # Agent C
│   │   ├── mineru.py      # Agent C
│   │   └── worker.py      # Agent F 集成
│   ├── dci/               # Agent D
│   │   ├── __init__.py
│   │   ├── tools.py
│   │   ├── prompts.py
│   │   └── agent.py
│   ├── web/               # Agent E
│   │   ├── __init__.py
│   │   ├── app.py
│   │   └── templates/     # 可后续再加
│   └── cli.py             # Agent E
├── ops/                   # Agent F
│   ├── paper-web.service
│   ├── paper-worker.service
│   ├── litestream.yml
│   └── deploy.md
└── tests/
    ├── conftest.py
    ├── test_db_paths_tasks.py
    ├── test_sources.py
    ├── test_filter_translate_digest.py
    ├── test_pdf_mineru_storage.py
    ├── test_dci_agent.py
    └── test_e2e_daily_flow.py
```

---

## 3. Wave 0：共享契约（必须先完成，由架构 Agent 执行）

### 3.1 交付物

| 文件 | 内容 |
|---|---|
| `paperbase/models.py` | `PaperStatus / PdfStatus / ParseStatus / TranslateStatus / SourceName / TaskType / TaskStatus` 枚举；`PaperDraft`、`PaperRecord` dataclass |
| `paperbase/db.py` | `connect() -> sqlite3.Connection`，自动执行 PRAGMA 和 schema migration |
| `paperbase/paths.py` | 所有文件路径的唯一计算入口 |
| `paperbase/tasks.py` | `enqueue_task / claim_task / finish_task / fail_task / list_pending`，幂等键 |
| `paperbase/llm.py` | `LLMClient` 协议、`LLMMessage / LLMResponse / LLMUsage`，统一重试与费用记录接口 |
| `paperbase/storage.py` | `ObjectStore` 协议：`put / get / delete / exists`；本地磁盘配额检查接口 |
| `config.example.toml` | 全量配置模板，其他 Agent 只读使用 |
| `pyproject.toml` | 依赖与 `paper` 命令入口占位 |

### 3.2 关键契约（其他 Agent 必须遵守，不得自行另造一套）

**路径规则（唯一入口 `paperbase.paths`）**

```text
DATA_DIR/
  papers.db
  md/{source}/{year}/{venue}/{paper_id}.md        # 英文原文，DCI 语料
  md/{source}/{year}/{venue}/{paper_id}.zh.md     # 中文全文
  pdf/hot/{paper_id}.pdf                          # 本地热 PDF
  pdf/uploads/{paper_id}.pdf                      # 手动上传原件
  tmp/{task_type}/{task_id}/                      # 临时目录，任务结束删除
  cache/{source}/...                              # 抓取/翻译缓存，可清空
```

**任务表幂等键**

```text
UNIQUE(paper_id, task_type, input_hash)
```

任何 Agent 创建任务前必须计算 `input_hash = sha1(paper_id + task_type + 输入内容标识)`。

**状态枚举（其他 Agent 不得扩展字符串）**

```text
PaperStatus:       new | in_queue | reading | done | later
PdfStatus:         none | needs_upload | downloading | downloaded | download_failed | cold
ParseStatus:       none | queued | uploading | parsing | downloading | done | failed
TranslateStatus:   none | queued | running | done | failed | skipped
TaskStatus:        queued | running | done | failed | cancelled
```

**LLM 调用必须走 `paperbase.llm.LLMClient.chat()`**

- 所有调用自动：重试、超时、token 统计、budget 标签、费用估算。
- 禁止各 Agent 直接 `requests.post` 调用 LLM。

**DCI 语料目录只读**

- DCI Agent 只能读 `md/` 和以 `mode=ro` 打开 `papers.db`。
- 工具执行层由 Agent D 自建，但必须调用 `paths.corpus_dir()` 获取路径。

---

## 4. 并行任务卡

### Agent A — 数据采集与导入

**负责 BDD：** 场景 1.1、1.2、1.3、1.4

**文件所有权：**

```text
paperbase/sources/__init__.py
paperbase/sources/acl.py
paperbase/sources/openalex.py
paperbase/sources/arxiv.py
paperbase/sources/import_legacy.py
tests/test_sources.py
```

**禁止修改：** `db.py / paths.py / tasks.py / models.py / config.py`

**任务：**

1. `import_legacy.py`：导入现有 `ACL-Anthology-Crawler/data/*.jsonl`，主键去重，输出导入报告。
2. `acl.py`：封装现有 `crawl_year.py` 逻辑，按年份增量抓取，输出 `PaperDraft`。
3. `openalex.py`：按 ISSN/期刊配置和增量时间窗口抓取，输出 `PaperDraft`。
4. `arxiv.py`：按分类/关键词抓取 arXiv API，输出 `PaperDraft`。
5. 实现统一接口：

```python
class PaperSource(Protocol):
    name: SourceName
    def fetch_incremental(self, since: datetime, state: dict) -> Iterator[PaperDraft]: ...
```

**完成标准（DoD）：**

- [ ] `paper init` 可导入历史 JSONL，重复执行不产生重复行
- [ ] ACL/OpenAlex 可手动增量抓取并写入 `papers` 表
- [ ] 失败请求重试 3 次，失败不阻塞其他论文
- [ ] 10 万条导入 ≤ 5 分钟（2C4G）
- [ ] `tests/test_sources.py` 通过

---

### Agent B — 兴趣筛选、摘要翻译与早报

**负责 BDD：** 场景 2.1、2.2、2.3、2.4（筛选和早报部分）；场景 5.2 的缓存规则

**文件所有权：**

```text
paperbase/pipeline/filter.py
paperbase/pipeline/translate.py  # 与 Agent C 协商分段接口
paperbase/pipeline/digest.py
tests/test_filter_translate_digest.py
```

**禁止修改：** Wave 0 契约文件

**任务：**

1. 把现有 `filter_papers.py` 的 `KEYWORD_GROUPS` 迁移为 YAML/TOML 规则，`match_paper(title, abstract) -> list[str]`。
2. 标题/摘要翻译：批处理、缓存、失败重试、每日预算熔断。
3. 生成每日 Markdown 早报：按标签分组、显示 PDF 状态、可点击链接。
4. 勾选论文：更新 `PaperStatus.in_queue` 并创建后续任务（通过 `tasks.enqueue_task`）。

**完成标准：**

- [ ] 规则可配置，重跑不覆盖用户标签
- [ ] 同内容不重复翻译、原文变化会重译
- [ ] 08:00 前生成早报（可配置）
- [ ] 勾选幂等，重复勾选不重复建任务
- [ ] `tests/test_filter_translate_digest.py` 通过

---

### Agent C — PDF 下载、手动上传、MinerU 解析与存储

**负责 BDD：** FEAT-03、FEAT-04 全部；FEAT-05 全文翻译；场景 9.1、9.2

**文件所有权：**

```text
paperbase/pipeline/pdf.py
paperbase/pipeline/mineru.py
paperbase/storage_impl.py        # 对象存储与 LRU 实现；协议在 storage.py
paperbase/pipeline/fulltext_translate.py  # 如与 Agent B 冲突，B 只保留元数据翻译
tests/test_pdf_mineru_storage.py
```

**禁止修改：** Wave 0 契约文件；`pipeline/filter.py / digest.py`

**任务：**

1. PDF 自动下载：开放链接检测、重试、MIME 校验、本地热目录。
2. 手动上传入口：大小/类型校验、SHA-256 去重、元数据匹配或创建 manual 记录。
3. MinerU API 客户端：提交/轮询/下载/校验，状态机写入 `tasks`。
4. 解析产物写 front-matter 和标准路径，临时文件原子替换。
5. 全文翻译：章节分块调用 `llm.chat`，输出 `.zh.md`，预算熔断。
6. 磁盘配额：本地 PDF 默认 6GB 热上限，LRU 淘汰到对象存储；Markdown 不自动删除。

**完成标准：**

- [ ] 手动上传 1MB PDF 到解析完成，状态全程可追踪
- [ ] 损坏 PDF 两次失败后标记 parse_failed，保留错误信息
- [ ] 同一文件重复上传不产生新记录
- [ ] 对象存储上传校验成功后才删除本地 PDF
- [ ] `tests/test_pdf_mineru_storage.py` 通过

---

### Agent D — DCI 全库问答

**负责 BDD：** FEAT-08 全部

**文件所有权：**

```text
paperbase/dci/__init__.py
paperbase/dci/tools.py
paperbase/dci/prompts.py
paperbase/dci/agent.py
tests/test_dci_agent.py
```

**禁止修改：** Wave 0 契约文件；不得自行实现数据库/路径逻辑

**任务：**

1. 工具白名单：`rg / grep / find / ls / sed / head / sqlite3(ro)`，参数化执行，禁止 shell 拼接。
2. 工具执行：只允许 `paths.corpus_dir()` 内读文件；SQLite 用 `mode=ro` URI。
3. Agent 循环：LLM tool-calling 循环，默认 ≤ 30 次工具调用，输出截断 12000 字符。
4. Prompt：移植 DCI 论文附录 C.1 模板，强制 `Explanation / Exact Answer / Confidence` 与 `[路径:行号]` 引用。
5. 三模式：单篇、全库（元数据预筛选 + rg）、多篇对比。
6. 无证据时 Confidence < 50%，禁止编造引用；记录 `qa_logs`。

**完成标准：**

- [ ] 单篇问答只访问指定论文文件
- [ ] 全库问答先用 SQLite 缩小范围，再 rg
- [ ] 工具命令不可写文件、不可联网、不可路径穿越
- [ ] 精确术语测试集召回 100%（20 个真实 Markdown 样本）
- [ ] 无证据测试集编造引用数 = 0
- [ ] `tests/test_dci_agent.py` 通过

---

### Agent E — CLI 与 Web 界面

**负责 BDD：** FEAT-06、FEAT-07；FEAT-00 的交互入口

**文件所有权：**

```text
paperbase/cli.py
paperbase/web/__init__.py
paperbase/web/app.py
paperbase/web/templates/...   # 可后续再加
```

**禁止修改：** Wave 0 契约文件；只调用其他 Agent 的公开函数

**任务：**

1. CLI：`paper init / fetch / today / queue / upload / read / ask / stats / doctor`。
2. FastAPI：早报页、论文库筛选页、详情页、阅读器（中英切换）、上传页、问答页。
3. 状态流转：`new → in_queue → reading → done / later`。
4. 标签、笔记、统计。
5. 阅读器引用定位（文件路径 + 行号锚点）。
6. `systemd` 启动文档配合 Agent F。

**完成标准：**

- [ ] 手机可完成：看早报、勾选、提问
- [ ] 10 万条元数据筛选 ≤ 200ms，详情页 ≤ 1s
- [ ] 未翻译/未解析论文展示正确提示和手动触发按钮
- [ ] CLI 与 Web 行为一致

---

### Agent F — Worker 调度、部署与端到端验收

**负责 BDD：** FEAT-09、FEAT-10；场景 0.1、0.2 端到端

**文件所有权：**

```text
paperbase/pipeline/worker.py
ops/paper-web.service
ops/paper-worker.service
ops/litestream.yml
ops/deploy.md
tests/test_e2e_daily_flow.py
```

**禁止修改：** Wave 0 契约文件；不实现业务逻辑，只做调度和运维

**任务：**

1. APScheduler：07:30 抓取 → 匹配 → 翻译 → 08:00 早报。
2. worker 任务循环：按优先级消费 `tasks` 表，进程重启可恢复。
3. systemd 两个服务：`paper-web`（≤512M）、`paper-worker`（≤1G）。
4. Litestream 备份到对象存储，恢复演练。
5. 磁盘 80% 提醒、90% 熔断。
6. 端到端测试：合成 1 天新数据，验证早报、勾选、解析、翻译、问答全链路。

**完成标准：**

- [ ] `paper-worker` 单实例运行，不重复调度
- [ ] worker 重启后任务可恢复，无重复 API 调用
- [ ] 从对象存储恢复到新目录后数据库可打开
- [ ] 磁盘阈值行为符合 BDD 场景 9.1
- [ ] `tests/test_e2e_daily_flow.py` 通过

---

## 5. 并行依赖关系

```text
Wave 0（契约层，串行前置）
  ├── Wave 1（可完全并行）
  │     ├── Agent A：数据采集
  │     ├── Agent B：筛选/翻译/早报
  │     ├── Agent C：PDF/MinerU/存储
  │     ├── Agent D：DCI 问答
  │     ├── Agent E：CLI/Web
  │     └── Agent F：worker 调度与 ops 骨架
  │
  ├── Wave 2（联调，依赖 Wave 1 公开函数）
  │     ├── E 接入 A/B/C/D 的真实实现
  │     ├── F 跑端到端
  │     └── 统一修复接口不匹配问题
  │
  └── Wave 3（验收）
        ├── 按 BDD P0 → P1 顺序逐条验收
        ├── 20G 磁盘演练
        └── 云服务器部署
```

---

## 6. 并行协作规则（重要）

1. **文件所有权唯一**：一个文件同一时间只允许一个 Agent 修改；需要跨文件改动时先在集成分支提交。
2. **Wave 0 冻结后，契约文件只允许架构 Agent 修改**；其他 Agent 发现契约不足，提 issue/说明，不得自行改。
3. **集成只通过公开函数和 SQLite**：
   - 读取路径一律 `paths.xxx()`
   - 创建任务一律 `tasks.enqueue_task()`
   - LLM 调用一律 `llm.chat()`
   - 不得直接拼其他 Agent 的文件路径字符串
4. **数据库 schema 由 `db.py` migration 管理**，禁止在业务代码里 `CREATE TABLE`。
5. **每个 Agent 必须有测试**，测试文件归该 Agent 所有，集成测试归 Agent F。
6. **提交粒度**：每张任务卡至少 3 次提交，提交信息带卡号，例如 `feat(A): legacy jsonl import`。
7. **DoD 未完成不得进入 Wave 2**；Wave 2 开始前由架构 Agent 逐个检查。

---

## 7. 时间估算（单机、业余开发）

| Wave | 内容 | 单人估算 | 6 Agent 并行估算 |
|---|---|---|---|
| 0 | 契约层 + 测试基线 | 0.5 天 | 0.5 天（串行） |
| 1 | A/B/C/D/E/F 并行开发 | 8–12 天 | 1.5–2.5 天 |
| 2 | 联调 | 2–3 天 | 1–2 天 |
| 3 | BDD 验收 + 部署 | 1–2 天 | 1–2 天 |
| 合计 | | 12–17 天 | 4–7 天 |

> 注：并行估算是理想值，实际取决于各 Agent 对契约理解的偏差，通常 Wave 2 会吃掉并行省下的时间。
