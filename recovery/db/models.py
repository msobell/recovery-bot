from datetime import date, datetime
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from recovery.db.session import Base


class GarminDaily(Base):
    __tablename__ = "garmin_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)

    # HRV
    hrv_status: Mapped[str | None] = mapped_column(String(32))
    hrv_rmssd: Mapped[float | None] = mapped_column(Float)
    hrv_baseline_low: Mapped[float | None] = mapped_column(Float)
    hrv_baseline_high: Mapped[float | None] = mapped_column(Float)

    # Heart rate
    resting_hr: Mapped[int | None] = mapped_column(Integer)

    # Sleep
    sleep_start: Mapped[datetime | None] = mapped_column(DateTime)
    sleep_end: Mapped[datetime | None] = mapped_column(DateTime)
    sleep_duration_min: Mapped[int | None] = mapped_column(Integer)
    sleep_deep_min: Mapped[int | None] = mapped_column(Integer)
    sleep_light_min: Mapped[int | None] = mapped_column(Integer)
    sleep_rem_min: Mapped[int | None] = mapped_column(Integer)
    sleep_awake_min: Mapped[int | None] = mapped_column(Integer)
    sleep_score: Mapped[int | None] = mapped_column(Integer)

    # Overnight stress (excludes daytime)
    overnight_stress_avg: Mapped[float | None] = mapped_column(Float)
    overnight_stress_qualifier: Mapped[str | None] = mapped_column(String(64))

    # Stress time-series derived metrics (sleep window only)
    stress_first_half_avg: Mapped[float | None] = mapped_column(Float)
    stress_second_half_avg: Mapped[float | None] = mapped_column(Float)
    stress_second_half_min: Mapped[int | None] = mapped_column(Integer)
    stress_recovery_delta: Mapped[float | None] = mapped_column(Float)
    stress_time_below_20_min: Mapped[int | None] = mapped_column(Integer)

    # Body battery
    body_battery_start: Mapped[int | None] = mapped_column(Integer)

    # Steps
    steps: Mapped[int | None] = mapped_column(Integer)

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class StravaActivity(Base):
    __tablename__ = "strava_activities"

    strava_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str | None] = mapped_column(Text)
    sport_type: Mapped[str | None] = mapped_column(String(64))
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[float | None] = mapped_column(Float)
    elevation_m: Mapped[float | None] = mapped_column(Float)
    avg_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    avg_power: Mapped[int | None] = mapped_column(Integer)
    suffer_score: Mapped[int | None] = mapped_column(Integer)
    perceived_exertion: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class GarminActivity(Base):
    __tablename__ = "garmin_activities"

    garmin_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str | None] = mapped_column(Text)
    sport_type: Mapped[str | None] = mapped_column(String(64))
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    avg_hr: Mapped[float | None] = mapped_column(Float)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # When true, this session has been hand-curated; sync must NOT touch its sets.
    manually_edited: Mapped[bool | None] = mapped_column(Integer, default=0)

    sets: Mapped[list["GarminStrengthSet"]] = relationship(
        "GarminStrengthSet", back_populates="activity", cascade="all, delete-orphan"
    )


class GarminStrengthSet(Base):
    __tablename__ = "garmin_strength_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    garmin_activity_id: Mapped[int] = mapped_column(Integer, ForeignKey("garmin_activities.garmin_id"), index=True)
    set_index: Mapped[int] = mapped_column(Integer)
    exercise_category: Mapped[str | None] = mapped_column(String(64))
    # user-overridden category; if set, UI shows this instead of exercise_category
    exercise_category_override: Mapped[str | None] = mapped_column(String(64))
    reps: Mapped[int | None] = mapped_column(Integer)
    weight_g: Mapped[float | None] = mapped_column(Float)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    start_time: Mapped[datetime | None] = mapped_column(DateTime)

    activity: Mapped["GarminActivity"] = relationship("GarminActivity", back_populates="sets")


class WeightEntry(Base):
    __tablename__ = "weight_entries"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    actual_weight_lbs: Mapped[float | None] = mapped_column(Float)
    weight_is_interpolated: Mapped[bool | None] = mapped_column(Integer)  # stored as 0/1
    trend_weight_lbs: Mapped[float | None] = mapped_column(Float)
    actual_fat_pct: Mapped[float | None] = mapped_column(Float)
    fat_is_interpolated: Mapped[bool | None] = mapped_column(Integer)
    trend_fat_pct: Mapped[float | None] = mapped_column(Float)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32))  # "garmin" | "strava"
    date_from: Mapped[date | None] = mapped_column(Date)
    date_to: Mapped[date | None] = mapped_column(Date)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
