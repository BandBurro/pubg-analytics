"""Spark and Delta Lake on the position layer — a deliberate learning exercise.

**This is not the recommended way to process this data.** `scripts/bench_positions.py`
showed DuckDB handling all 207M rows on one laptop: the hardest query in the whole
project takes 30 seconds, and a 270-million-pair range join takes under three.
Spark is not warranted at this scale, and the crossover is around 300k-3M matches
(10x-100x today's corpus).

So why run it at all? Two reasons, both honest:

1. **The API and Delta semantics are worth knowing** — DataFrame operations, lazy
   evaluation, partitioning, ACID transactions, time travel, MERGE, compaction.
   Those transfer to every real lakehouse regardless of engine.
2. **The comparison is the lesson.** Running both on identical data and reporting
   the numbers is more useful than assuming distributed always wins. It usually
   does not, on one machine.

Run: uv run python scripts/spark_delta_exercise.py
"""

import os
import shutil
import time
from pathlib import Path

# Homebrew keeps openjdk@17 keg-only, so it is not on PATH by default.
os.environ.setdefault("JAVA_HOME", "/opt/homebrew/opt/openjdk@17")

import duckdb  # noqa: E402
from delta import configure_spark_with_delta_pip  # noqa: E402
from pyspark.sql import SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql.window import Window  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POSITIONS = f"{ROOT}/data/bronze/player_position/*.parquet"
DELTA_PATH = f"{ROOT}/data/delta/player_position"
# Feature demos run on a slice: writing 207M rows to Delta locally costs minutes
# and gigabytes, and teaches nothing the slice doesn't.
DEMO_MATCHES = 200


def build_session() -> SparkSession:
    builder = (
        SparkSession.builder.appName("pubg-position-exercise")
        # local[*] means "one JVM, all cores" — this is the whole point of the
        # comparison: same hardware DuckDB just used, different engine.
        .master("local[*]")
        .config("spark.driver.memory", "8g")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
        # Default is 200 partitions, which on ~200M rows and 10-ish cores means
        # a lot of small tasks. Left explicit so the number is a decision.
        .config("spark.sql.shuffle.partitions", "64")
    )
    return configure_spark_with_delta_pip(builder).getOrCreate()


def head_to_head(spark: SparkSession) -> None:
    """The same rotation-distance window query, both engines, full 207M rows."""
    print("\n=== head to head: rotation distance over every position row ===")

    con = duckdb.connect(":memory:")
    con.execute("pragma disable_progress_bar")
    start = time.perf_counter()
    duck = con.execute(
        f"""
        with d as (
          select sqrt(pow(x - lag(x) over w, 2) + pow(y - lag(y) over w, 2)) step
          from read_parquet('{POSITIONS}')
          where not is_bot
          window w as (partition by match_id, account_id order by event_ts)
        ) select count(*), sum(step) from d where step is not null
        """
    ).fetchone()
    duck_s = time.perf_counter() - start
    print(f"  DuckDB : {duck_s:>7.2f}s   steps={duck[0]:,}")

    start = time.perf_counter()
    w = Window.partitionBy("match_id", "account_id").orderBy("event_ts")
    df = spark.read.parquet(f"{ROOT}/data/bronze/player_position/").filter(~F.col("is_bot"))
    stepped = df.withColumn(
        "step",
        F.sqrt(
            F.pow(F.col("x") - F.lag("x").over(w), 2)
            + F.pow(F.col("y") - F.lag("y").over(w), 2)
        ),
    ).filter(F.col("step").isNotNull())
    spark_row = stepped.agg(F.count("step"), F.sum("step")).collect()[0]
    spark_s = time.perf_counter() - start
    print(f"  Spark  : {spark_s:>7.2f}s   steps={spark_row[0]:,}")

    faster = "DuckDB" if duck_s < spark_s else "Spark"
    ratio = max(duck_s, spark_s) / max(min(duck_s, spark_s), 1e-9)
    print(f"\n  -> {faster} is {ratio:.1f}x faster on identical data and hardware.")
    # Same answer from both engines is the only reason the timing means anything.
    assert spark_row[0] == duck[0], f"engines disagree: {spark_row[0]} vs {duck[0]}"
    print("  -> both engines returned the same step count, so the comparison is real.")


def delta_features(spark: SparkSession) -> None:
    """ACID, time travel, MERGE and compaction — what a table format buys."""
    print(f"\n=== Delta Lake features (slice: {DEMO_MATCHES} matches) ===")
    if Path(DELTA_PATH).exists():
        shutil.rmtree(DELTA_PATH)

    all_pos = spark.read.parquet(f"{ROOT}/data/bronze/player_position/")
    keep = [r[0] for r in all_pos.select("match_id").distinct().limit(DEMO_MATCHES).collect()]
    slice_df = all_pos.filter(F.col("match_id").isin(keep))

    start = time.perf_counter()
    slice_df.write.format("delta").mode("overwrite").save(DELTA_PATH)
    n = spark.read.format("delta").load(DELTA_PATH).count()
    print(f"  wrote {n:,} rows to Delta in {time.perf_counter() - start:.1f}s (version 0)")

    # 1. ACID + time travel. Deleting rows creates a new version; the old one is
    #    still readable, which plain Parquet cannot do.
    from delta.tables import DeltaTable

    table = DeltaTable.forPath(spark, DELTA_PATH)
    table.delete(F.col("in_vehicle"))
    after = spark.read.format("delta").load(DELTA_PATH).count()
    v0 = spark.read.format("delta").option("versionAsOf", 0).load(DELTA_PATH).count()
    print(f"  after deleting in-vehicle rows : {after:,}")
    print(f"  reading versionAsOf=0          : {v0:,}  <- history survived the delete")

    # 2. MERGE. The upsert that makes incremental loading safe: re-running a load
    #    updates what changed instead of duplicating everything.
    updates = slice_df.filter(F.col("in_vehicle")).limit(5000)
    table.alias("t").merge(
        updates.alias("s"),
        "t.match_id = s.match_id and t.account_id = s.account_id and t.event_ts = s.event_ts",
    ).whenNotMatchedInsertAll().execute()
    merged = spark.read.format("delta").load(DELTA_PATH).count()
    print(f"  after MERGE of 5,000 rows      : {merged:,}")

    # 3. Compaction. Many small files is the classic lakehouse performance bug;
    #    OPTIMIZE rewrites them into fewer, larger ones.
    #
    # Count ACTIVE files from the transaction log, not files on disk. OPTIMIZE
    # writes compacted files and tombstones the originals — it does not delete
    # them until VACUUM runs — so a disk-level glob shows the count going *up*
    # and reports the opposite of what happened.
    def active_files() -> int:
        return spark.sql(f"describe detail delta.`{DELTA_PATH}`").collect()[0]["numFiles"]

    before = active_files()
    on_disk_before = len(list(Path(DELTA_PATH).glob("*.parquet")))
    table.optimize().executeCompaction()
    after = active_files()
    on_disk_after = len(list(Path(DELTA_PATH).glob("*.parquet")))
    print(f"  active files : {before} -> {after} after OPTIMIZE")
    print(f"  files on disk: {on_disk_before} -> {on_disk_after} (tombstoned, awaiting VACUUM)")

    hist = spark.sql(f"describe history delta.`{DELTA_PATH}`").select("version", "operation")
    print("\n  transaction log:")
    for row in sorted(hist.collect(), key=lambda r: r["version"]):
        print(f"    v{row['version']}  {row['operation']}")


def main() -> None:
    spark = build_session()
    spark.sparkContext.setLogLevel("ERROR")
    print(f"Spark {spark.version}, master={spark.sparkContext.master}")
    try:
        head_to_head(spark)
        delta_features(spark)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
