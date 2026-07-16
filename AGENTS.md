# Repository Guidelines

## Project Structure & Module Organization

Application code uses a `src` layout under `src/kol_search/`. `cli.py` exposes Typer commands, `web.py` serves the FastAPI dashboard, and `db.py` owns SQLite persistence. Discovery and scoring logic belongs in `src/kol_search/discovery/`; X integrations implement the shared interface in `src/kol_search/twitter/`. Jinja templates and browser assets live in `templates/` and `static/`. Keep domain settings in `config/`, curated inputs in `seeds/`, offline samples in `fixtures/`, and utilities in `scripts/`. Tests live in `tests/test_*.py`. Runtime databases and exports belong in ignored `data/` and `output/` directories.

## Build, Test, and Development Commands

Requires Python 3.11 or newer.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"       # editable install with pytest tooling
cp .env.example .env           # create local configuration; never commit it
kol-search web --reload         # dashboard at http://127.0.0.1:8765
kol-search discover "DeFi researcher" --backend mock --limit 30
pytest                          # run the complete suite
pytest --cov=kol_search         # report coverage
```

Install `.[ai]` or `.[twscrape]` only when working on those optional integrations. The mock backend is the default for deterministic local development.

## Coding Style & Naming Conventions

Follow established Python style: four-space indentation, type annotations, `snake_case` for functions and modules, and `PascalCase` for classes and Pydantic models. Keep provider-specific behavior behind the Twitter backend interface. Prefer small, explicit functions and concise docstrings for public or non-obvious behavior. No formatter or linter is configured, so match surrounding code and keep imports grouped as standard library, third party, then local.

## Testing Guidelines

Use pytest and name tests `test_<behavior>`. Use `tmp_path` for database tests and fixtures/mock backends for network behavior; tests must not require live credentials. Add focused regression tests for backend failover, persistence, scoring, contact safety, or web-route changes. Run the full suite before opening a PR. Coverage has no enforced threshold, but new branches should be exercised.

## Commit & Pull Request Guidelines

The current history uses short, imperative, sentence-case subjects (for example, `Add Crypto KOL search with OpenCLI fallback`). Keep each commit scoped to one change. PRs should explain user-visible behavior, configuration changes, and verification performed; link issues and include dashboard screenshots for UI changes. Call out schema, seed-catalog, or backend compatibility impacts.

## Security & Configuration

Never commit `.env`, API keys, browser profiles, databases, or exports. Preserve the contact collector's SSRF protections and public-source-only policy. Keep the unauthenticated dashboard bound to `127.0.0.1`; do not expose it directly to the public internet.
