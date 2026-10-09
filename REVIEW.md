# REVIEW.md

Review of the starter `app/main.py`. All of these are fixed in my version.

| # | Where | What is wrong | How I'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Client is created without `tz_aware=True`, so every datetime read from Mongo is naive; no server selection timeout, so a dead Mongo blocks for 30 s. | Comparing a stored `punch_in` with an aware datetime raises `TypeError`; `/health` hangs when Mongo is down. | `tz_aware=True`, `serverSelectionTimeoutMS=5000`. |
| 2 | `GET /health` | Always returns 200, it never talks to MongoDB. | Stop mongod, call `/health`: still 200 (contract says 503). | `ping` the server, 503 on `PyMongoError`. |
| 3 | `create_employee` | "find then insert" is a race and there is no unique index. | Fire 10 parallel POSTs with the same `emp_code`: several 201s and duplicate rows. | Unique index on `emp_code`; `insert_one` and turn `DuplicateKeyError` into 409. |
| 4 | `EmployeeIn` | No validation at all: `emp_code="x"`, `email="abc"`, `shift_start="9:30"`, `joined_on="2026-02-30"`, equal shift times are all accepted. | POST any of those, expect 422, get 201. | Field patterns and validators for emp_code, email, HH:MM, a real `joined_on` date, and `shift_start != shift_end`. |
| 5 | `create_employee` | `created_at = datetime.now()` is local and naive, and is returned as a datetime instead of epoch ms. | Response has `"created_at": "2026-07-06T10:00:00"`. | UTC time, stored as BSON date, returned as epoch milliseconds. |
| 6 | `list_employees` | `skip = page * page_size` skips the first page. | `GET /employees?page=1` misses the first 20 employees. | `(page - 1) * page_size`. |
| 7 | `list_employees` | `total` counts the whole collection, ignoring the department filter. | `?department=Sales` returns 3 items and `total=500`. | `count_documents(query)` with the same filter. |
| 8 | `list_employees` | No sort, so page boundaries are not stable. | Same employee appears on two pages, or on none. | `.sort("emp_code", 1)`. |
| 9 | `list_employees` / `list_attendance` | `page` and `page_size` are not validated. | `page=-1` gives a negative skip and a 500; `page_size=100000` is accepted. | `Query(ge=1)` and `Query(ge=1, le=100)`, so bad values are 422. |
| 10 | `punch_in` | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises `TypeError`, a 500. | Punch in with `emp_code="NOPE"`. | `get_employee()` raises 404. |
| 11 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server's local zone and gives a naive datetime, so `date` is the server date, not the IST date. Milliseconds are not cut to seconds. | Server running in UTC: a punch-in at 02:00 IST on the 6th (20:30 UTC on the 5th) is saved with date `2026-07-05`. | `ms_to_dt()` builds a UTC datetime truncated to whole seconds; the date comes from `attendance_date()` in IST. |
| 12 | `PunchInIn` | `punched_at: Optional[int]` accepts seconds (`1783312500`), floats and negative numbers, and `if body.punched_at` treats `0` as "not given". | Send a seconds value: record lands in January 1970. | Strict int with `100000000000 <= x <= 4102444800000`, everything else is 422. |
| 13 | `punch_in` | `status` is a free string: `ABSENT`, `LEAVE` or `banana` are accepted. | POST `"status": "banana"`. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 14 | `punch_in` | "find then insert" race and no unique index. | Two simultaneous punch-ins both return 201 and two documents exist. | Unique `(emp_code, date)` index; 409 on `DuplicateKeyError`. |
| 15 | `punch_in` | Overnight shifts are ignored. | Shift 22:00-06:00, punch-in at 01:00 IST is saved on the new day instead of the day the shift started. | `attendance_date()` implements R1. |
| 16 | `punch_in` response | Returns `"id"` (the ObjectId) and datetimes instead of epoch ms; legacy documents come without `history`/`half_day`. | Look at the JSON. | `record_out()` builds the response explicitly, converts instants, and defaults missing fields. |
| 17 | `compute_late_minutes` | Floors to minutes first and then checks `> 10`. `replace()` is done on the punch-in's own date and zone. Overnight shifts are wrong. | 09:40:01 on a 09:30 shift should be 10 minutes late, gives 0 (`int(10.01) > 10` is false). | Compare **seconds** with the 10-minute grace (`seconds > 600`), then `seconds // 60`, with the shift start built in IST on the attendance date. |
| 18 | `compute_work_hours` | Python `round()` is banker's rounding on a float. | 29250 s = 8.125 h gives `8.12`, R4 wants `8.13`. | `Decimal` division, `ROUND_HALF_UP`. |
| 19 | `compute_overtime` | No 30-minute minimum, ignores overnight shifts (end is next day), builds the end time without seconds. | Punch-out 10 minutes after shift end returns 10 instead of 0; for a 22:00-06:00 shift the end is placed on the wrong day, so a normal 06:40 punch-out gives a wrong value. | Rewritten per R3 using the attendance date, `+1 day` for overnight, and the `>= 30` rule. |
| 20 | `list_attendance` | Loads the **whole** collection into memory, sorts in Python (by date only, no `emp_code` tie-break), and paginates the Python list. No index. | 100k records: slow and memory heavy. | `find().sort().skip().limit()` backed by indexes. `total` from `count_documents`. |
| 21 | `list_attendance` | No validation for dates (`date_from=abc`), `date_from > date_to`, or `status`. | `?status=banana` just returns an empty list. | Date parsing and `Status` enum, bad values are 422. |
| 22 | whole file | No indexes. | Check `explain`: `COLLSCAN`. | `create_indexes()` runs at startup (idempotent). |

## Looked at and decided it is fine
- `load_dotenv()`: by default it does not override variables that already exist, so real environment variables still win.
- String comparison `date >= "2026-07-01"`: `YYYY-MM-DD` strings sort the same as the dates, so the range queries are correct.
- `doc.pop("_id")` after `insert_one`: correct idea (the `_id` must not leak), I just replaced it with explicit response builders.
- The default `mongodb://localhost:27017` in `os.getenv(...)`: only a local fallback, the real values come from the environment.
