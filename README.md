# Employee Attendance & Analytics API

FastAPI + MongoDB. All the code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup, nothing else to do. A local `.env` also works, real environment variables take priority.

## Notes
- All endpoints from the contract are implemented, including `/admin/explain/{endpoint}`.
- Analytics are MongoDB aggregation pipelines. Only the final rounding (R8, half-up) is done in Python.
- Some choices where the contract was open:
  - `moving_avg_7d` is the mean of the raw (unrounded) daily rates, rounded once at the end.
  - A punch-out earlier than the employee's punch-in gives 422; an employee with no punch-in at all gives 404.
  - Two concurrent PATCH calls on one record: the loser gets 409 (optimistic check on the record and its history length).
  - The explain endpoint and the real endpoints use the same `hint`, so the plan is always an index scan.
