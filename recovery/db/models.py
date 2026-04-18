from datetime import date, datetime
from sqlalchemy import Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
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

    # Body battery
    body_battery_start: Mapped[int | None] = mapped_column(Integer)

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
