"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read for convenience, real env vars win)

Everything lives in this one file, in this order:
  1. setup (mongo connection, indexes)
  2. small helper functions (time, rules R1-R5, rounding)
  3. request models
  4. employee + attendance endpoints
  5. analytics pipelines + endpoints
  6. explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True -> every datetime we read back from Mongo carries UTC tzinfo
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME = 30  # R3
HALF_DAY_LIMIT = 4.5  # R5

# index names are written out so that hints / explain can refer to them
IDX_LOG_EMP_DATE = "emp_code_1_date_1"
IDX_LOG_DATE_EMP = "date_-1_emp_code_1"
IDX_LOG_STATUS_DATE = "status_1_date_-1_emp_code_1"
IDX_EMP_DEPT = "department_1_emp_code_1"
IDX_EMP_JOINED = "joined_on_1"

_indexes_ready = False


def create_indexes():
    """Creates every index we need. create_index is idempotent, so running it on each start is fine."""
    global _indexes_ready
    logs = db.attendance_logs
    emps = db.employees

    # one record per (employee, day): punch-in race guard, monthly report, $lookup joins
    logs.create_index([("emp_code", 1), ("date", 1)], name=IDX_LOG_EMP_DATE, unique=True)
    # GET /attendance default sort, leaderboard + trend (date range / one day)
    logs.create_index([("date", -1), ("emp_code", 1)], name=IDX_LOG_DATE_EMP)
    # GET /attendance?status=...  (equality on status, then the same sort)
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name=IDX_LOG_STATUS_DATE)

    # emp_code must be unique: this is what makes duplicate creates safe
    emps.create_index([("emp_code", 1)], name="emp_code_1", unique=True)
    # list by department sorted by emp_code, and the department filter in analytics
    emps.create_index([("department", 1), ("emp_code", 1)], name=IDX_EMP_DEPT)
    # headcount (R9): joined_on <= end of month
    emps.create_index([("joined_on", 1)], name=IDX_EMP_JOINED)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        create_indexes()
    except PyMongoError as exc:  # mongo not reachable yet; /health will retry
        log.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
def invalid(name: str, message: str):
    """Raise a normal FastAPI 422 for a value we validate by hand."""
    raise RequestValidationError(
        [{"type": "value_error", "loc": ("query", name), "msg": message, "input": None}]
    )


# ---- time conversion ------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """epoch milliseconds -> UTC datetime, cut down to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # should not happen (tz_aware client) but be safe: Mongo stores UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    dt = datetime.now(UTC)
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


# ---- rounding (R4, R8) ----------------------------------------------------- #
def round_half_up(value, places: int) -> float:
    """Half-up rounding. We first round to 10 places to throw away float noise like 0.1249999999."""
    d = Decimal(repr(round(float(value), 10)))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ---- shift helpers --------------------------------------------------------- #
def minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp: dict) -> bool:
    return minutes_of(emp["shift_end"]) <= minutes_of(emp["shift_start"])


def ist_moment(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in, except early-morning punches of an overnight shift."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        seconds_today = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_today < minutes_of(emp["shift_end"]) * 60:
            day = day - timedelta(days=1)
    return day


def calc_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only if MORE than 10:00 after shift start; then count from shift start."""
    start = ist_moment(day, emp["shift_start"])
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def calc_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: whole minutes after shift end, only if at least 30."""
    end = ist_moment(day, emp["shift_end"])
    if is_overnight(emp):
        end = end + timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME else 0


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up. Decimal keeps it exact."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(emp: dict, day: date, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """late_minutes / work_hours / overtime_minutes / half_day for a presence record (R2-R5)."""
    result = {
        "late_minutes": calc_late_minutes(punch_in, day, emp),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = calc_work_hours(punch_in, punch_out)
        result["work_hours"] = hours
        result["overtime_minutes"] = calc_overtime(punch_out, day, emp)
        result["half_day"] = hours < HALF_DAY_LIMIT
    return result


# ---- converting stored documents to API shape ------------------------------ #
def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        before, after = change.get("from"), change.get("to")
        if field in ("punch_in", "punch_out"):
            before, after = dt_to_ms(before), dt_to_ms(after)
        changes[field] = {"from": before, "to": after}
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def record_out(doc: dict) -> dict:
    """Attendance document -> API record. Missing fields get their defaults (legacy data)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# ---- parsing calendar strings ---------------------------------------------- #
def parse_day(value: str, name: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        invalid(name, "date must look like YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid(name, "not a real calendar date")


def parse_month(value: str):
    """'2026-07' -> (first_day, last_day) as date objects."""
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        invalid("month", "month must look like YYYY-MM")
    year, month = int(value[:4]), int(value[5:])
    try:
        first = date(year, month, 1)
    except ValueError:
        invalid("month", "year out of range")
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def count_working_days(start: date, end: date) -> int:
    """Mon-Fri days from start to end, both included (R7)."""
    if end < start:
        return 0
    total_days = (end - start).days + 1
    weeks, rest = divmod(total_days, 7)
    count = weeks * 5
    for i in range(rest):
        if (start + timedelta(days=i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
# epoch milliseconds: strict int, so floats / strings / seconds-looking values give 422
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_real(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on is not a real date")
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Employees + attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb is not reachable")
    if not _indexes_ready:  # mongo was down at startup, try again now
        try:
            create_indexes()
        except PyMongoError as exc:
            log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # unique index on emp_code decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    when = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(when, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": when,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": calc_late_minutes(when, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # no "check first": the unique (emp_code, date) index lets exactly one insert succeed
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    when = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    logs = db.attendance_logs

    # most recent record whose punch-in is at or before the punch-out
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": when}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        # maybe the punch-out time is simply earlier than the employee's punch-in
        if logs.find_one({"emp_code": body.emp_code, "punch_in": {"$gt": when}}) is not None:
            raise HTTPException(422, "punched_at is before the punch_in")
        raise HTTPException(404, "no punch-in found for this employee")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if when <= rec["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if when - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    day = date.fromisoformat(rec["date"])
    changes = derived_fields(emp, day, rec["punch_in"], when)
    changes["punch_out"] = when

    # only update if the record is still open: of two simultaneous punch-outs one gets None back
    updated = logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": rec["date"],
            "punch_in": rec["punch_in"],
            "punch_out": None,
        },
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size):
    """Builds the find() for GET /attendance. Shared with the explain endpoint."""
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    sort = [("date", -1), ("emp_code", 1)]
    skip = (page - 1) * page_size

    # pick the index that matches the filters
    if emp_code:
        hint = IDX_LOG_EMP_DATE
    elif status:
        hint = IDX_LOG_STATUS_DATE
    else:
        hint = IDX_LOG_DATE_EMP
    return query, sort, skip, page_size, hint


def check_date_filters(date_from: Optional[str], date_to: Optional[str]):
    d_from = parse_day(date_from, "date_from") if date_from else None
    d_to = parse_day(date_to, "date_to") if date_to else None
    if d_from and d_to and d_from > d_to:
        invalid("date_from", "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_date_filters(date_from, date_to)
    query, sort, skip, limit, hint = attendance_list_query(
        emp_code, date_from, date_to, status, page, page_size
    )
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort(sort).skip(skip).limit(limit).hint(hint)
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: str, body: RegularizeIn):
    day = parse_day(att_date, "date")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # ---- what the record looks like now ----
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    # ---- what it should look like after the correction ----
    new_status = body.status or old["status"]
    asked_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    asked_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if new_status in ("ABSENT", "LEAVE"):
        if asked_in is not None or asked_out is not None:
            raise HTTPException(422, f"{new_status} records cannot have punch times")
        new_in, new_out = None, None
        new_derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        new_in = asked_in or old["punch_in"]
        new_out = asked_out or old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if attendance_date(new_in, emp) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
        new_derived = derived_fields(emp, day, new_in, new_out)

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **new_derived}

    # a request that does not change status / punch times is rejected
    asked_change = (
        new["status"] != old["status"]
        or new["punch_in"] != old["punch_in"]
        or new["punch_out"] != old["punch_out"]
    )
    if not asked_change:
        raise HTTPException(422, "nothing to change")

    # history only lists the fields that really changed
    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {f: {"from": old[f], "to": new[f]} for f in fields if old[f] != new[f]}
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic locking: the update only applies if the record is exactly as we read it
    # (same status/punches, and history still has the same length). A concurrent edit makes
    # this match nothing and the loser gets 409, so no history entry is ever lost.
    history_len = len(rec.get("history") or [])
    still_same = {
        "emp_code": emp_code,
        "date": att_date,
        "status": old["status"],
        "punch_in": old["punch_in"],
        "punch_out": old["punch_out"],
        f"history.{history_len}": {"$exists": False},
    }
    updated = db.attendance_logs.find_one_and_update(
        still_same,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 5. Analytics (all done with aggregation pipelines)
# --------------------------------------------------------------------------- #
# Small reusable pieces of pipeline expressions -------------------------------- #
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}
IS_PRESENT = {"$in": ["$status", PRESENCE]}
# a present record is worth 1 day, a half day is worth 0.5, anything else 0
PRESENCE_WEIGHT = {
    "$cond": [
        IS_PRESENT,
        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
        0,
    ]
}
# same, but only Monday-Friday counts (R7). `date` is a "YYYY-MM-DD" string.
WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
WEEKDAY_PRESENCE_WEIGHT = {"$cond": [WEEKDAY, PRESENCE_WEIGHT, 0]}


def run_aggregate(collection: str, pipeline: list, hint: str) -> list:
    return list(db[collection].aggregate(pipeline, hint=hint))


# ---- employee monthly -------------------------------------------------------- #
def monthly_pipeline(emp_code: str, month: str):
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": WEEKDAY_PRESENCE_WEIGHT},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE},
                "total_overtime_minutes": {"$sum": OVERTIME},
            }
        },
    ]
    return "attendance_logs", pipeline, IDX_LOG_EMP_DATE


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = parse_month(month)
    emp = get_employee(emp_code)

    _, pipeline, hint = monthly_pipeline(emp_code, month)
    rows = run_aggregate("attendance_logs", pipeline, hint)
    totals = rows[0] if rows else {}

    # working days count from the joining date for people who joined mid-month (R7)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined), last)
    present_days = totals.get("present_days", 0)

    attendance_pct = None
    if working_days > 0:
        pct = Decimal(repr(float(present_days))) / Decimal(working_days) * 100
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(totals.get("leave_days", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


# ---- department summary ------------------------------------------------------ #
def summary_pipeline(month: str, department: Optional[str]):
    """
    Starts from `employees` (so people with no logs are still counted), joins that
    month's logs onto each employee, then groups by department.
    """
    first, last = parse_month(month)

    employee_match = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department:
        employee_match["department"] = department

    # runs on the logs of ONE employee (joined through emp_code) for the month
    month_logs = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$project": {
                "_id": 0,
                "present": WEEKDAY_PRESENCE_WEIGHT,
                "late": LATE,
                "is_late": IS_LATE,
                # work hours only count for present-status records that have a value
                "hours_ok": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]},
                "hours": {
                    "$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, "$work_hours", 0]
                },
                "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
            }
        },
    ]

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": month_logs,
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                # "$logs.present" is an array, the inner $sum adds it up per employee
                "present_days": {"$sum": {"$sum": "$logs.present"}},
                "hours_sum": {"$sum": {"$sum": "$logs.hours"}},
                "hours_count": {"$sum": {"$sum": "$logs.hours_ok"}},
                "late_count": {"$sum": {"$sum": "$logs.is_late"}},
                "total_late_minutes": {"$sum": {"$sum": "$logs.late"}},
                "leave_count": {"$sum": {"$sum": "$logs.leave"}},
                "on_duty_count": {"$sum": {"$sum": "$logs.on_duty"}},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    hint = IDX_EMP_DEPT if department else IDX_EMP_JOINED
    return "employees", pipeline, hint


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    parse_month(month)
    _, pipeline, hint = summary_pipeline(month, department)
    rows = run_aggregate("employees", pipeline, hint)

    items = []
    for r in rows:
        avg_hours = None
        if r["hours_count"] > 0:
            avg_hours = round_half_up(r["hours_sum"] / r["hours_count"], 2)  # average over records
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": round_half_up(r["present_days"], 2),
                "avg_work_hours": avg_hours,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# ---- late leaderboard -------------------------------------------------------- #
def leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": LATE},
                "late_count": {"$sum": IS_LATE},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # logs of employees that do not exist disappear here ($unwind drops empty arrays)
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank only inside the department
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return "attendance_logs", pipeline, IDX_LOG_DATE_EMP


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    parse_month(month)
    _, pipeline, hint = leaderboard_pipeline(month, limit, department)
    rows = run_aggregate("attendance_logs", pipeline, hint)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# ---- department trend -------------------------------------------------------- #
def trend_range(from_value: str, to_value: str):
    d_from = parse_day(from_value, "from")
    d_to = parse_day(to_value, "to")
    if d_to < d_from:
        invalid("to", "'to' must not be before 'from'")
    days = (d_to - d_from).days + 1
    if days > 92:
        invalid("to", "range must not be longer than 92 days")
    return d_from, days


def trend_pipeline(department: str, d_from: date, days: int):
    """
    One row per calendar day. The days are generated inside the database with $range, so
    weekends and empty days are there even when no log exists. Then a window function
    gives the 7-day moving average.
    """
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    pipeline = [
        # 1) the employees of the department (one document with two arrays)
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # 2) the list of days "YYYY-MM-DD", one per day of the range
        {
            "$project": {
                "_id": 0,
                "codes": 1,
                "joined": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "i",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}},
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        # 3) for each day, add up that day's logs of the department's employees
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$days", "codes": "$codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {"$group": {"_id": None, "present": {"$sum": PRESENCE_WEIGHT}, "late": {"$sum": IS_LATE}}},
                ],
                "as": "agg",
            }
        },
        # 4) headcount (R9) and the plain numbers of the day
        {
            "$project": {
                "_id": 0,
                "date": "$days",
                "headcount": {"$size": {"$filter": {"input": "$joined", "cond": {"$lte": ["$$this", "$days"]}}}},
                "present_count": {"$ifNull": [{"$first": "$agg.present"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$agg.late"}, 0]},
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, 5]
                },
            }
        },
        # 5) attendance rate: only on working days with people, otherwise null
        {
            "$addFields": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # 6) moving average over this row and the 6 before it ($avg skips the nulls)
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, IDX_EMP_DEPT


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, days = trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")

    _, pipeline, hint = trend_pipeline(department, d_from, days)
    rows = run_aggregate("employees", pipeline, hint)
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if r["rate"] is None else round_half_up(r["rate"], 4),
            "moving_avg_7d": None if r["moving"] is None else round_half_up(r["moving"], 4),
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain
# --------------------------------------------------------------------------- #
def require(value, name: str):
    if value is None or value == "":
        invalid(name, "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # build the exact same query / pipeline the real endpoint builds
    if endpoint == "attendance_list":
        check_date_filters(date_from, date_to)
        query, sort, skip, lim, hint = attendance_list_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": dict(sort),
            "skip": skip,
            "limit": lim,
            "hint": hint,
        }
    else:
        if endpoint == "employee_monthly":
            collection, pipeline, hint = monthly_pipeline(require(emp_code, "emp_code"), require(month, "month"))
        elif endpoint == "department_summary":
            collection, pipeline, hint = summary_pipeline(require(month, "month"), department)
        elif endpoint == "late_leaderboard":
            collection, pipeline, hint = leaderboard_pipeline(require(month, "month"), limit, department)
        else:  # department_trend
            d_from, days = trend_range(require(from_, "from"), require(to, "to"))
            collection, pipeline, hint = trend_pipeline(require(department, "department"), d_from, days)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    # explain contains dates / ObjectIds, so go through bson's json_util to get plain JSON
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result)),
    }
