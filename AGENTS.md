# Repository Guidelines

## Project Structure & Module Organization

- `paperbase/` is the installable Python package and `paperbase/cli.py` exposes the `paper` command.
- `paperbase/pipeline/` contains PDF, parsing, translation, digest, filtering, and worker stages; `sources/` contains ACL, arXiv, and OpenAlex importers.
- `paperbase/web/` holds the FastAPI application and its bundled HTML/CSS; `paperbase/dci/` contains corpus search and question-answering logic.
- `tests/` contains pytest tests, generally grouped by subsystem or pipeline phase. `docs/` records requirements and design decisions; `ops/` contains deployment and service files.
- `data/`, `papers/`, and `ACL-Anthology-Crawler/data/` are runtime or imported content. Treat them as local data, not source modules.

## Build, Test, and Development Commands

```bash
python3 -m venv venv && . venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest                         # run the complete suite
python -m pytest tests/test_web.py       # run one focused module
paper web --port 8000                    # run the local FastAPI UI
paper worker --once                      # process one queued task
```

Copy `config.example.toml` to `config.toml` and use `.env.example` for local environment variables before running data or API-backed commands.

## Coding Style & Naming Conventions

Use Python 3.11-compatible code, four-space indentation, and clear type hints where practical. Keep modules, functions, and variables in `snake_case`, classes in `PascalCase`, and constants in `UPPER_SNAKE_CASE`. Preserve existing docstring and import organization patterns. No formatter or linter is configured; keep changes small and make `pytest` pass.

## Testing Guidelines

Name files `test_*.py` and test functions `test_*`. Prefer the existing fixtures and temporary paths over touching repository data. Tests should be deterministic and must not require live LLM, MinerU, or source APIs. There is no stated coverage gate; add regression tests for behavior changes and run the narrow test first, followed by the full suite.

## Commit & Pull Request Guidelines

Use the established Conventional Commit style, such as `feat(web): add paper filter` or `fix(pipeline): retry failed parse`. Keep subjects concise and scoped. Pull requests should explain user-visible and data-model effects, list validation commands, call out configuration or deployment changes, link related issues, and include UI screenshots for web changes.

## Security & Configuration

Never commit `config.toml`, `.env`, API keys, or generated paper/PDF content. Review path and account-scoping changes carefully, and update `ops/deploy.md` when operational behavior changes.
