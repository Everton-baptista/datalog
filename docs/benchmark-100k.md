# 100k-Span Local Pilot Benchmark

Executed on November 9, 2026. The workload was designed as a local pilot with 100,000 spans, 100,000 distinct attribute values, 32 services, and 4 concurrent read clients. Synthetic records were distributed across 8 days to exercise both 24-hour and 7-day query windows.

The authenticated HTTP ingestion path sustained **14,960 spans/s** in 1,000-span gzip batches, with **100,000 persisted, zero rejected, and zero lost**. Batch ingestion p95 was **83.535 ms**. The **394 concurrent read queries** during ingestion recorded a p95 of **31.534 ms**. The first read probe after the final batch ACK confirmed visibility in **3.681 ms** (including query execution). Gzip wire transfer consumed **22.49 bytes per span** on this repetitive workload.

The storage comparison evaluated the exact same persisted records, three identical queries, four clients, and 48 measurements per engine per window (lookup by trace ID, service aggregation over time, and high-cardinality attribute filtering) after warm-up:

- **PostgreSQL 18.6**: p95 of **12.009 ms** (24 h) and **49.520 ms** (7 d), using **983.45 bytes/span** with Datalog's JSONB schema and indexes.
- **ClickHouse 26.9.2.1 via chDB 4.4.0**: p95 of **14.453 ms** (24 h) and **17.016 ms** (7 d), using **30.29 bytes/span** on MergeTree (shared embedded instance).
- **Parquet (ZSTD) + DuckDB 2.0.0**: p95 of **7.203 ms** (24 h) and **7.094 ms** (7 d), using **10.83 bytes/span** (excluding object-storage ingestion, compaction and freshness overhead).

Retention removed 12,500 spans at the 7-day cutoff (retaining 87,500) and retained 12,500 spans at the 24-hour cutoff, matching the input timestamp distribution exactly.

This local measurement (AArch64, 20 visible CPUs) serves as a single-node baseline. Datalog remains on PostgreSQL 18 for operational simplicity and transactional consistency.

Raw numbers and versions are stored in [benchmarks/results-100k.json](../benchmarks/results-100k.json). To reproduce against `TEST_DATABASE_URL` (uses a disposable schema and temporary directories):

```sh
python -m pip install -e ".[benchmark]"
python -m benchmarks.pilot --spans 100000 --clients 4
```
