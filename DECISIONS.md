# Implementation Decisions

1. Indexes: I created a unique index on `(emp_code, date)` to prevent duplicate attendance records and support employee/date lookups. Date-oriented compound indexes support attendance filtering and analytics. Employee indexes support unique employee-code lookups and department/join-date queries.

2. Concurrent punch-ins: The unique attendance index is the final safeguard against duplicate records for the same employee and date. When simultaneous requests race, one insert succeeds with `201`; conflicting requests return `409`. My concurrency test returned one `201` and nine `409` responses.

3. Leaderboard ties: Employees are sorted by total late minutes descending, then employee code ascending. Equal totals share the same competition rank; the next rank is skipped. The limit is applied after ranking.

4. Employees without logs: Department headcount must be calculated from employees whose `joined_on` is on or before the reporting date, independently of attendance records. Such employees count toward headcount even if they have no attendance logs.

5. Scaling: For 100 times the data, I would measure execution statistics on representative large datasets, review index selectivity and query plans, avoid unnecessary document fetches, and optimize aggregation pipelines based on measured bottlenecks.