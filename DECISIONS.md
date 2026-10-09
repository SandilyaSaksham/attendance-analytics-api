# DECISIONS.md

1. **Indexes.** On `attendance_logs`: unique `(emp_code, date)` stops a double punch-in and serves the monthly report and joins; `(date desc, emp_code)` serves the default list order and date-range queries; `(status, date desc, emp_code)` serves the status filter. On `employees`: unique `emp_code`, `(department, emp_code)`, and `joined_on` for headcount. I rejected an index on `late_minutes`: the leaderboard sums per employee, so it would never be used.

2. **Punch-in race.** Both requests work out the same IST date and call `insert_one` directly, with no "find first". The unique `(emp_code, date)` index lets one insert win; the other gets `DuplicateKeyError`, which I return as 409.

3. **Ties.** I rank with `$rank` before applying the limit and return everyone with rank `<= limit`. So employees tied at the cutoff are all included, and there can be more rows than `limit`.

4. **Headcount.** The summary pipeline starts from `employees` and uses `$lookup` to attach the month's logs. Someone with no logs gets an empty array, but `$group` still counts the employee.

5. **100x data.** I would keep a small pre-computed daily summary per employee, updated on every change, instead of scanning a month of raw logs.
