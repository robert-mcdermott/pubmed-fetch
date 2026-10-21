# AGENTS.md

- Use `uv run` to run anything (never bare `python`), and `uv add` for dependencies (never `pip`).
- Package code is in `src/pubmed_fetch/`: `query` (query building, dates), `client` (E-utilities), `parse` (XML -> records), `fulltext` (PMC Cloud Service, downloads), `icite`, `outputs` (sinks), `state`, `pipeline` (`run()`), `cli`.
- Verify changes with:
  - `uv run -m pytest -q` (offline; must not touch the network)
  - `uv run ruff check . && uv run ruff format --check .`
  - `uv run mypy`
  - `PUBMED_FETCH_LIVE=1 uv run -m pytest -q -m live` when changing anything that talks to NCBI/PMC/iCite
- Record changes to the output must update `src/pubmed_fetch/schema/output.schema.json` (tests validate against it) and the CHANGELOG; bump `SCHEMA_VERSION` in `_meta.py` for breaking changes.
- `sample_config.ini` must equal `cli.SAMPLE_CONFIG` (a test checks this).
- The agent skill in `skills/pubmed-fetch/` (`SKILL.md`, `reference.md`) documents the CLI for other agents; update it when options or output fields change. Keep its frontmatter portable: only `name` (must match the folder name) and `description` (at most 1024 characters), with no tool-specific fields.
- Test fixtures in `tests/fixtures/pubmed_efetch_sample.xml` are real efetch responses covering edge cases (book, retraction, erratum, MedlineDate, season, ORCID, grants, collective authors, structured abstract).
