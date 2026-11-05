from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, field_validator


class SequenceState(str, Enum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    EXPOSING = "EXPOSING"
    DOWNLOADING = "DOWNLOADING"
    INTERVAL = "INTERVAL"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class StorageDestination(str, Enum):
    COMPUTER = "computer"
    CAMERA = "camera"


class CameraDisplayMode(str, Enum):
    LIVEVIEW = "liveview"
    PLAYBACK = "playback"


class FocusDirection(str, Enum):
    NEAR = "near"
    FAR = "far"


class CameraInfo(BaseModel):
    connected: bool = False
    name: str | None = None
    port: str | None = None
    simulation: bool = False
    ownership_error: bool = False
    message: str | None = None


class ConfigProperty(BaseModel):
    path: str | None = None
    available: bool = False
    current: str | None = None
    choices: list[str] = Field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    message: str | None = None


class CameraConfig(BaseModel):
    iso: ConfigProperty = Field(default_factory=ConfigProperty)
    aperture: ConfigProperty = Field(default_factory=ConfigProperty)
    shutter_speed: ConfigProperty = Field(default_factory=ConfigProperty)
    exposure_mode: ConfigProperty = Field(default_factory=ConfigProperty)
    capture_target: ConfigProperty = Field(default_factory=ConfigProperty)
    viewfinder: ConfigProperty = Field(default_factory=ConfigProperty)
    application_mode: ConfigProperty = Field(default_factory=ConfigProperty)
    manual_focus: ConfigProperty = Field(default_factory=ConfigProperty)
    focus_mode: ConfigProperty = Field(default_factory=ConfigProperty)
    liveview_focus: ConfigProperty = Field(default_factory=ConfigProperty)


class CameraDisplayRequest(BaseModel):
    mode: CameraDisplayMode


class LiveViewStatus(BaseModel):
    active: bool
    frame_id: int = 0
    error: str | None = None
    simulation: bool = False


class FocusRequest(BaseModel):
    direction: FocusDirection
    step: int = Field(ge=1, le=3)


class FocusResult(BaseModel):
    active: bool
    direction: FocusDirection
    step: int
    amount: int
    simulation: bool = False


class CameraMediaItem(BaseModel):
    path: str
    name: str
    folder: str
    kind: str
    previewable: bool = True


class SequenceRequest(BaseModel):
    exposure: float = Field(gt=0, le=86_400)
    interval: float = Field(ge=0, le=86_400)
    shots: int = Field(ge=1, le=100_000)
    iso: str | None = None
    aperture: str | None = None
    storage_destination: StorageDestination = StorageDestination.COMPUTER
    output_subdirectory: str = ""

    @field_validator("iso", "aperture")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 for char in value):
            raise ValueError("control characters are not allowed")
        return value

    @field_validator("output_subdirectory")
    @classmethod
    def safe_subdirectory(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        candidate = PurePosixPath(value.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("output must be a relative folder below the configured root")
        if any(part in {"", "."} for part in candidate.parts):
            raise ValueError("invalid output folder")
        if any(ord(char) < 32 for char in value):
            raise ValueError("control characters are not allowed")
        return str(candidate)


class SequenceStatus(BaseModel):
    state: SequenceState = SequenceState.IDLE
    active: bool = False
    sequence_completed: int = 0
    sequence_total: int = 0
    current_frame: int = 0
    exposure_elapsed: float = 0
    exposure_duration: float = 0
    interval_remaining: float = 0
    last_completed: str | None = None
    current_filename: str | None = None
    next_filename: str | None = None
    error: str | None = None
    output_directory: str | None = None
    storage_destination: StorageDestination = StorageDestination.COMPUTER
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    level: str = "info"
    message: str


class RuntimeInfo(BaseModel):
    output_root: str
    simulation: bool
    ptp_helper_configured: bool


def resolve_output_directory(root: Path, subdirectory: str) -> Path:
    root = root.expanduser().resolve()
    output = (root / subdirectory).resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ValueError("output folder escapes the configured root") from exc
    return output
