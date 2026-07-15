# MLB API MCP — CLAUDE.md

A Model Context Protocol (MCP) server exposing MLB statistics and baseball data
(via `python-mlb-statsapi` and `pybaseball`) to MCP-compatible clients. Runs
over stdio (default) or streamable HTTP.

## Stack

- **Python 3.10+** (CI targets 3.12; `pyproject.toml` floor is 3.10)
- **FastMCP** (`fastmcp>=2.10.6`) — the third-party `fastmcp` package, not the
  official MCP SDK's `mcp.server.fastmcp`. Builds its ASGI app via `http_app()`.
- **python-mlb-statsapi** (`mlbstatsapi`) — primary data source
- **pybaseball** — Statcast queries (`statcast`, `statcast_batter`, `statcast_pitcher`)
- **uvicorn + Starlette** — HTTP transport and custom routes
- **uv** — dependency management and task runner

## Tooling

- **uv** — dependency management (`uv sync`, `uv run`)
- **ruff** — linter and formatter (config in `pyproject.toml` `[tool.ruff]`)
- **mypy** — static type checking (config in `pyproject.toml` `[tool.mypy]`)
- **pytest + pytest-asyncio + pytest-cov** — tests, **80% coverage gate**

## Entry points

```bash
uv sync --extra dev        # dev tooling (ruff, mypy, pre-commit)
uv sync --extra test       # test deps (pytest, coverage)
uv run python main.py      # stdio transport (default; for Smithery / Claude Desktop)
uv run python main.py --http   # HTTP transport on :8000 (PORT env overrides)
```

HTTP endpoints: `/` (redirect → `/docs`), `/health`, `/info`, `/tools`, `/docs`,
`/mcp` (the MCP protocol endpoint).

## Structure

```
main.py             FastMCP instance, custom HTTP routes, argparse entry point
mlb_api.py          22 MLB tools registered onto the FastMCP instance via setup_mlb_tools(mcp)
generic_api.py      2 generic tools (get_current_date, get_current_time) via setup_generic_tools(mcp)
current_mlb_teams.csv   team-name → team-id lookup, read by mlb_api.py with a BARE
                        relative path (open("current_mlb_teams.csv")) — the process
                        cwd must be the repo root for this to resolve
Dockerfile          uv-based image; runs `python main.py --http` on :8000
smithery.yaml       Smithery deployment config (container runtime, HTTP startCommand)
tests/
  test_mlb_api.py   79 tests (pytest-asyncio, asyncio_mode=auto)
  run_coverage.py   convenience test/coverage runner
railway-wrapper/    Cloudflare-Access-fronted HTTP deployment (see below)
.github/workflows/
  ci.yml            lint (ruff + mypy) → test (pytest + coverage badge)
```

All tools are plain functions registered with `@mcp.tool()` inside
`setup_mlb_tools(mcp)` / `setup_generic_tools(mcp)`; these run at import time
(main.py calls them at module scope, not under `if __name__ == "__main__"`),
so `import main` yields a fully-populated `mcp` instance.

## railway-wrapper/ — remote HTTP deployment

A thin ASGI wrapper (`server.py`) that runs this repo's own `main.py` tools over
streamable HTTP behind Cloudflare Access JWT verification. Deployed via Railway's
"Shared Monorepo" pattern — **do not set a Root Directory** on the Railway
service; the build/start commands are:

```
pip install -r railway-wrapper/requirements.txt
python railway-wrapper/server.py
```

Two deliberate, load-bearing hacks in `server.py` (both documented inline and
exempted from ruff's `E402` via `pyproject.toml`):

1. It inserts the repo root onto `sys.path` and `chdir`s there before importing
   `main` — so `import main` resolves and `mlb_api.py`'s bare
   `open("current_mlb_teams.csv")` works regardless of Railway's cwd.
2. Those delayed module-level imports are why `E402` is suppressed for this file.

`requirements.txt` mirrors the root `pyproject.toml` deps plus `PyJWT[crypto]`
and `starlette`; the wrapper is **not** installed as a package (it imports
`main`/`mlb_api`/`generic_api` directly from the repo root).

## CI

`.github/workflows/ci.yml` has two jobs on push/PR to `main` and `add_tests`:

1. **lint** — `ruff check .`, `ruff format --check .`, `mypy` (uses
   `uv sync --extra dev`). Must pass before test runs.
2. **test** — `uv sync --extra test && uv run pytest --cov-report=xml`, then
   regenerates the coverage badge in `README.md` (same-repo PRs/pushes only;
   external forks get a step-summary report instead).

Dependabot (`.github/dependabot.yml`) opens weekly `chore(deps)` PRs for pip,
docker, and github-actions ecosystems.

## Dev workflow

```bash
uv sync --extra dev --extra test      # install everything
uv run ruff check .                   # lint
uv run ruff format --check .          # format check (use `ruff format .` to fix)
uv run mypy                           # type-check (configured files list in pyproject.toml)
uv run pytest -q                      # tests + coverage (80% gate enforced)
uv run pre-commit install             # optional: local hooks (ruff, trailing-whitespace, etc.)
```

Pre-commit config (`.pre-commit-config.yaml`) runs ruff + ruff-format and the
standard pre-commit-hooks (trailing-whitespace, end-of-file-fixer, check-yaml,
check-added-large-files).

## Testing notes

- `asyncio_mode = "auto"` — async test functions need no `@pytest.mark.asyncio`.
- Coverage source is `mlb_api.py` + `generic_api.py`; the 80% gate is in
  `pyproject.toml` `addopts` (`--cov-fail-under=80`).
- Tests mock the `mlbstatsapi`/`pybaseball` network layer — no live API calls.
