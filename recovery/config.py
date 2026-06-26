from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_CONFIG_PATH = Path.cwd() / "config.toml"


@dataclass
class UserConfig:
    name: str = "User"
    timezone: str = "America/New_York"


@dataclass
class GarminConfig:
    email: str = ""


@dataclass
class StravaConfig:
    client_id: str = ""
    client_secret: str = ""


@dataclass
class SyncConfig:
    sync_time: str = "08:00"
    backfill_days: int = 730


@dataclass
class EquipmentConfig:
    sauna: bool = False
    squat_rack: bool = False
    bench_press: bool = False
    dumbbells: bool = False
    kettlebells: bool = False
    bands: bool = False
    pullup_bar: bool = False
    barbell: bool = False
    cable_machine: bool = False
    leg_press: bool = False
    treadmill: bool = False
    stationary_bike: bool = False
    rowing_machine: bool = False
    pool: bool = False
    dumbbell_max_kg: float = 0.0

    def available(self) -> list[str]:
        skip = {"dumbbell_max_kg"}
        return [k for k, v in self.__dict__.items() if v is True and k not in skip]

    def summary(self) -> str:
        items = self.available()
        if not items:
            return "No equipment configured."
        names = [i.replace("_", " ") for i in items]
        if self.dumbbells and self.dumbbell_max_kg:
            names = [f"dumbbells (up to {self.dumbbell_max_kg}kg)" if n == "dumbbells" else n for n in names]
        return ", ".join(names)


@dataclass
class RecoveryConfig:
    hrv_low_pct: float = 0.85
    hrv_high_pct: float = 1.10
    rhr_high_offset: int = 5
    sleep_min_hours: float = 7.0
    overnight_stress_low: int = 25
    overnight_stress_high: int = 45


@dataclass
class TrendWeightConfig:
    share_url: str = ""


@dataclass
class UIConfig:
    port: int = 8080
    default_trend_days: int = 30


@dataclass
class KnowledgeConfig:
    # OCR for scanned/image-only PDF pages. Needs ANTHROPIC_API_KEY in the env;
    # if the key is absent, scanned pages are skipped with a warning regardless
    # of this flag. Text PDFs never use OCR and need no key.
    ocr_enabled: bool = True
    ocr_model: str = "claude-haiku-4-5"   # fast + cheap + accurate for OCR
    max_pdf_mb: int = 25                   # reject uploads larger than this


@dataclass
class Config:
    user: UserConfig = field(default_factory=UserConfig)
    garmin: GarminConfig = field(default_factory=GarminConfig)
    strava: StravaConfig = field(default_factory=StravaConfig)
    trendweight: TrendWeightConfig = field(default_factory=TrendWeightConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    equipment: EquipmentConfig = field(default_factory=EquipmentConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)


def load(path: Path | None = None) -> Config:
    path = path or _DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Config()

    if u := raw.get("user"):
        cfg.user = UserConfig(**{k: v for k, v in u.items() if hasattr(UserConfig, k)})

    if g := raw.get("garmin"):
        cfg.garmin = GarminConfig(**{k: v for k, v in g.items() if hasattr(GarminConfig, k)})

    if s := raw.get("strava"):
        cfg.strava = StravaConfig(**{k: v for k, v in s.items() if hasattr(StravaConfig, k)})

    if tw := raw.get("trendweight"):
        cfg.trendweight = TrendWeightConfig(**{k: v for k, v in tw.items() if hasattr(TrendWeightConfig, k)})

    if s := raw.get("sync"):
        cfg.sync = SyncConfig(**{k: v for k, v in s.items() if hasattr(SyncConfig, k)})

    if e := raw.get("equipment"):
        flat = {k: v for k, v in e.items() if k != "dumbbell_max_kg"}
        if dmk := e.get("dumbbell_max_kg"):
            flat["dumbbell_max_kg"] = dmk.get("value", 0.0)
        cfg.equipment = EquipmentConfig(**{k: v for k, v in flat.items() if hasattr(EquipmentConfig, k)})

    if r := raw.get("recovery"):
        cfg.recovery = RecoveryConfig(**{k: v for k, v in r.items() if hasattr(RecoveryConfig, k)})

    if ui := raw.get("ui"):
        cfg.ui = UIConfig(**{k: v for k, v in ui.items() if hasattr(UIConfig, k)})

    if k := raw.get("knowledge"):
        cfg.knowledge = KnowledgeConfig(**{kk: vv for kk, vv in k.items() if hasattr(KnowledgeConfig, kk)})

    return cfg


_cfg: Config | None = None


def get() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load()
    return _cfg
