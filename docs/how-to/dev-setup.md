# Dev setup

Set up Tunapi for local development and run the checks.

## Clone and run

```bash
git clone https://github.com/banteg/tunapi
cd tunapi

# Run directly with uv (installs deps automatically)
uv run tunapi --help
```

## Install locally (optional)

```bash
uv tool install .
tunapi --help
```

## Run checks

```bash
uv run pytest
uv run ruff check src tests
uv run ty check .

# Or all at once
just check
```

