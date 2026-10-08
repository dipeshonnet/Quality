# Analytics and import refactoring

The analytics API now aggregates reviewed audits by reporting day and defects by
category in SQL. Both queries use the same account, process, date, and user-access
filters. This removes the query parameter per audit and keeps Python allocations
proportional to reporting days and categories rather than audit and defect counts.
`qcc/analytics.py` contains the statistical calculations independently of routes
and database access. API response fields and metric definitions are preserved.

XLSX historical imports consume worksheet rows directly into result records,
without retaining an additional list of every raw worksheet row. The workbook
closes even when parsing fails. The result records themselves still reside in
memory for the existing preview workflow.

## Verification on October 8, 2026

- All 34 unit/regression tests passed.
- Python compilation, both application JavaScript syntax checks, and diff
  whitespace checks passed.
- Added coverage for restrictive SQL parameter limits, aggregate totals and
  Pareto category fallback, empty analytics, XLSX values, and workbook cleanup.
- Existing coverage checks account authorization, date ranges, scoring,
  sampling, reporting, and security behavior.

## Local SQLite measurement

A synthetic history contained 20,000 reviewed audits across 60 reporting days
and 19,999 defect records. The original analytics function from Git HEAD and the
refactored function returned exactly equal responses. Five warm calls were timed
per implementation; Python peak allocations were measured separately with
`tracemalloc`.

| Measurement | Original | Refactored |
| --- | ---: | ---: |
| Median analytics call | 0.6790 seconds | 0.1710 seconds |
| Peak Python allocation | 39.65 MiB | 0.07 MiB |

This local workload was approximately four times faster. These measurements do
not include HTTP/network latency or database-internal memory. PostgreSQL was not
available for runtime verification; the queries use compatible aggregates and an
explicit timestamp-to-text cast for daily labels. No schema migration is needed.
