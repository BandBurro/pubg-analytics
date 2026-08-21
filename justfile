default:
    @just --list

# One-time setup: create the venv and install everything.
setup:
    uv sync --extra dev
    @test -f .env || (cp .env.example .env && echo "created .env — paste your PUBG_API_KEY into it")

# Find new match ids.
discover:
    uv run pubg discover

# Download pending matches + telemetry.
fetch limit="200":
    uv run pubg fetch --limit {{limit}}

# Discover then fetch. This is what the scheduler calls.
collect limit="200":
    uv run pubg run --limit {{limit}}

status:
    uv run pubg status

# Shred raw telemetry JSON into typed Parquet.
shred limit="0" batch="250":
    uv run pubg shred --limit {{limit}} --batch {{batch}}

# Build the warehouse (Silver models + tests).
build:
    cd dbt && uv run dbt build --profiles-dir .

# Full pipeline: warehouse -> ratings -> rating models.
# The rating engine sits between two dbt runs: it reads fact_player_match and
# writes a table that dbt then models.
pipeline:
    cd dbt && uv run dbt build --profiles-dir . --exclude tag:ratings
    uv run pubg rate
    cd dbt && uv run dbt build --profiles-dir .

# Run the skill-rating engine.
rate:
    uv run pubg rate

# Run only the dbt tests.
dbt-test:
    cd dbt && uv run dbt test --profiles-dir .

# Open a SQL shell on the warehouse.
sql:
    uv run python -c "import duckdb; duckdb.connect('data/warehouse.duckdb').sql('show all tables').show()"

test:
    uv run pytest -q

lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# How much raw data have we accumulated?
du:
    @du -sh data/raw 2>/dev/null || echo "no data yet"

# --- scheduled collection (launchd) ---

schedule-start:
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matheuswark.pubg-collect.plist
    @echo "scheduled — runs every 2h, and once immediately"

schedule-stop:
    launchctl bootout gui/$(id -u)/com.matheuswark.pubg-collect
    @echo "stopped"

schedule-status:
    @launchctl list | grep pubg-collect || echo "not scheduled"

# Trigger a run now without waiting for the timer.
schedule-kick:
    launchctl kickstart -k gui/$(id -u)/com.matheuswark.pubg-collect

logs:
    @tail -n 30 data/logs/collect.log 2>/dev/null || echo "no logs yet"

logs-err:
    @tail -n 30 data/logs/collect.err.log 2>/dev/null || echo "no errors logged"

# Expand collection along player histories (what ratings need).
cohort limit="200":
    uv run pubg cohort --limit {{limit}}

# Estimator studies against synthetic ground truth.
study:
    uv run pubg study
