from __future__ import annotations

import asyncio
import re
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .camera import CameraError, CameraOwnershipError, GPhotoCamera
from .models import (
    CameraConfig,
    CameraDisplayMode,
    CameraInfo,
    CameraMediaItem,
    LogEntry,
    LiveViewStatus,
    SequenceRequest,
    SequenceState,
    SequenceStatus,
    StorageDestination,
    resolve_output_directory,
)
from .platform_support import KeepAwake


class SequenceConflictError(RuntimeError):
    pass


class SequenceValidationError(ValueError):
    pass


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)


def next_sequence_number(directory: Path) -> int:
    highest = 0
    if directory.exists():
        for path in directory.iterdir():
            match = re.match(r"^light_(\d+)\.", path.name, flags=re.IGNORECASE)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


class SequenceController:
    def __init__(
        self,
        camera: GPhotoCamera,
        output_root: Path,
        *,
        keep_awake: KeepAwake | None = None,
    ) -> None:
        self.camera = camera
        self.output_root = output_root.expanduser().resolve()
        self.keep_awake = keep_awake or KeepAwake()
        self.camera_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.status = SequenceStatus()
        self.camera_info = camera.info
        self.camera_config = camera.config
        self.logs: deque[LogEntry] = deque(maxlen=500)
        self.debug_logs: deque[LogEntry] = deque(maxlen=500)
        self.events = EventBroker()
        self._gallery_paths: set[str] = set()
        self._thumbnail_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._thumbnail_cache_bytes = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _touch(self) -> None:
        self.status.updated_at = datetime.now(timezone.utc)
        self.events.publish({"type": "status", "data": self.status.model_dump(mode="json")})

    def log(self, message: str, level: str = "info") -> None:
        entry = LogEntry(message=message, level=level)
        self.logs.append(entry)
        self.events.publish({"type": "log", "data": entry.model_dump(mode="json")})

    async def debug(self, message: str) -> None:
        entry = LogEntry(message=message, level="debug")
        self.debug_logs.append(entry)
        self.events.publish({"type": "debug", "data": entry.model_dump(mode="json")})

    async def refresh_camera(self) -> tuple[CameraInfo, CameraConfig]:
        async with self._state_lock:
            if self.running:
                raise SequenceConflictError("Camera is busy with an imaging sequence")
            async with self.camera_lock:
                await self._refresh_camera_unlocked()
        self.events.publish({"type": "camera", "data": self.camera_info.model_dump(mode="json")})
        return self.camera_info, self.camera_config

    async def _refresh_camera_unlocked(self) -> None:
        # A persistent Live View process owns the USB/PTP session; stop it before
        # redetection, then restore local camera controls below.
        await self.camera.stop_liveview(send_off=False)
        info = await self.camera.detect()
        self.camera_info = info
        if info.connected:
            try:
                self.camera_config = await self.camera.get_camera_config()
                if self.camera_config.viewfinder.available and self.camera_config.application_mode.available:
                    try:
                        await self.camera.set_display_mode(CameraDisplayMode.PLAYBACK)
                    except CameraError as exc:
                        self.log(f"Camera connected, but local playback controls could not be enabled: {exc}", "warning")
                self.log(f"Camera detected: {info.name}")
            except CameraError as exc:
                self.camera_config = CameraConfig()
                self.camera_info = info.model_copy(
                    update={
                        "connected": False,
                        "ownership_error": isinstance(exc, CameraOwnershipError),
                        "message": str(exc),
                    }
                )
                self.log(f"Camera configuration failed: {exc}", "error")
        else:
            self.camera_config = CameraConfig()
            self.log(info.message or "Camera disconnected", "warning")

    async def release_camera_services(self, release_callback) -> tuple[str, CameraInfo, CameraConfig]:
        async with self._state_lock:
            if self.running:
                raise SequenceConflictError("Camera is busy with an imaging sequence")
            async with self.camera_lock:
                detail = await release_callback()
                await self._refresh_camera_unlocked()
        self.events.publish({"type": "camera", "data": self.camera_info.model_dump(mode="json")})
        return detail, self.camera_info, self.camera_config

    def _validate_choices(self, request: SequenceRequest) -> None:
        for label, value, prop in (
            ("ISO", request.iso, self.camera_config.iso),
            ("aperture", request.aperture, self.camera_config.aperture),
        ):
            if prop.available:
                if value is None:
                    raise SequenceValidationError(f"{label} is required for this camera")
                if value not in prop.choices:
                    raise SequenceValidationError(f"Unsupported {label}: {value}")
            elif value is not None:
                raise SequenceValidationError(f"{label} is not exposed by the camera")
        if self.camera.storage_choice(request.storage_destination) is None:
            destination = "computer storage" if request.storage_destination == StorageDestination.COMPUTER else "the camera memory card"
            raise SequenceValidationError(f"The connected camera does not expose a capture target for {destination}")

    async def set_camera_display(self, mode: CameraDisplayMode) -> CameraConfig:
        async with self._state_lock:
            if self.running:
                raise SequenceConflictError("Live View and playback controls are unavailable during a sequence")
            if not self.camera_info.connected:
                raise SequenceValidationError("No camera is connected")
            async with self.camera_lock:
                await self.camera.set_display_mode(mode)
                self.camera_config = self.camera.config
        label = "Live View enabled on camera" if mode == CameraDisplayMode.LIVEVIEW else "Camera controls/playback enabled"
        self.log(label)
        self.events.publish({"type": "config", "data": self.camera_config.model_dump(mode="json")})
        return self.camera_config

    def liveview_status(self) -> LiveViewStatus:
        return LiveViewStatus(
            active=self.camera.liveview_running,
            frame_id=self.camera.liveview_frame_id,
            error=self.camera.liveview_error,
            simulation=self.camera_info.simulation,
        )

    async def next_liveview_frame(self, after_frame_id: int) -> tuple[int, bytes, str]:
        if not self.camera.liveview_running:
            raise CameraError(self.camera.liveview_error or "Live View is not running")
        return await self.camera.next_liveview_frame(after_frame_id)

    async def list_camera_media(self) -> list[CameraMediaItem]:
        async with self._state_lock:
            if self.running:
                raise SequenceConflictError("Camera playback is unavailable during an imaging sequence")
            if not self.camera_info.connected:
                raise SequenceValidationError("No camera is connected")
            async with self.camera_lock:
                await self.camera.prepare_for_gallery()
                paths = await self.camera.list_camera_files()
        self._gallery_paths = set(paths)
        self._thumbnail_cache = OrderedDict(
            (path, value) for path, value in self._thumbnail_cache.items() if path in self._gallery_paths
        )
        self._thumbnail_cache_bytes = sum(len(value[0]) for value in self._thumbnail_cache.values())
        image_extensions = {".jpg", ".jpeg", ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".dng", ".tif", ".tiff"}
        video_extensions = {".mov", ".mp4", ".avi"}
        items = []
        for path in reversed(paths):
            suffix = Path(path).suffix.lower()
            kind = "image" if suffix in image_extensions else "video" if suffix in video_extensions else "file"
            items.append(
                CameraMediaItem(
                    path=path,
                    name=Path(path).name,
                    folder=str(Path(path).parent),
                    kind=kind,
                    previewable=kind == "image",
                )
            )
        self.log(f"Camera gallery loaded: {len(items)} file{'s' if len(items) != 1 else ''}")
        return items

    async def get_camera_thumbnail(self, path: str) -> tuple[bytes, str]:
        if path not in self._gallery_paths:
            raise SequenceValidationError("Refresh the gallery before requesting this camera file")
        async with self._state_lock:
            if self.running:
                raise SequenceConflictError("Camera playback is unavailable during an imaging sequence")
            cached = self._thumbnail_cache.get(path)
            if cached is not None:
                self._thumbnail_cache.move_to_end(path)
                return cached
            async with self.camera_lock:
                result = await self.camera.get_camera_thumbnail(path)
            self._thumbnail_cache[path] = result
            self._thumbnail_cache_bytes += len(result[0])
            self._thumbnail_cache.move_to_end(path)
            while len(self._thumbnail_cache) > 120 or self._thumbnail_cache_bytes > 48 * 1024 * 1024:
                _, evicted = self._thumbnail_cache.popitem(last=False)
                self._thumbnail_cache_bytes -= len(evicted[0])
            return result

    async def start(self, request: SequenceRequest) -> SequenceStatus:
        async with self._state_lock:
            if self.running:
                raise SequenceConflictError("An imaging sequence is already running")
            if not self.camera_info.connected:
                raise SequenceValidationError("No camera is connected")
            self._validate_choices(request)
            output: Path | None = None
            if request.storage_destination == StorageDestination.COMPUTER:
                try:
                    output = resolve_output_directory(self.output_root, request.output_subdirectory)
                except ValueError as exc:
                    raise SequenceValidationError(str(exc)) from exc
                output.mkdir(parents=True, exist_ok=True)
            self._stop_event = asyncio.Event()
            self.status = SequenceStatus(
                state=SequenceState.PREPARING,
                active=True,
                sequence_total=request.shots,
                exposure_duration=request.exposure,
                output_directory=str(output) if output is not None else "Camera memory card",
                storage_destination=request.storage_destination,
            )
            self._touch()
            self._task = asyncio.create_task(self._run(request, output), name="astroremote-sequence")
        return self.status.model_copy(deep=True)

    async def stop(self) -> SequenceStatus:
        async with self._state_lock:
            if not self.running:
                if self.status.state != SequenceState.ERROR:
                    self.status.state = SequenceState.IDLE
                self.status.active = False
                self._touch()
                return self.status.model_copy(deep=True)
            self._stop_event.set()
            self.status.state = SequenceState.STOPPING
            self._touch()
            self.log("Stop requested; safely closing Bulb")
        return self.status.model_copy(deep=True)

    async def wait(self, timeout: float | None = None) -> None:
        task = self._task
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def shutdown(self) -> None:
        await self.stop()
        if self.running:
            try:
                await self.wait(timeout=25)
            except asyncio.TimeoutError:
                assert self._task is not None
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, CameraError):
                    pass
        try:
            await self.camera.stop_liveview()
        except CameraError:
            pass
        await self.keep_awake.stop()

    async def _progress(self, elapsed: float) -> None:
        self.status.exposure_elapsed = elapsed
        if self._stop_event.is_set():
            self.status.state = SequenceState.STOPPING
        self._touch()

    async def _downloading(self) -> None:
        self.status.state = SequenceState.DOWNLOADING if not self._stop_event.is_set() else SequenceState.STOPPING
        self._touch()
        action = "downloading" if self.status.storage_destination == StorageDestination.COMPUTER else "saving to camera card"
        self.log(f"Exposure {self.status.current_frame}/{self.status.sequence_total} complete; {action}")

    @staticmethod
    def _display_name(number: int) -> str:
        return f"light_{number:04d}.*"

    async def _run(self, request: SequenceRequest, output: Path | None) -> None:
        original_capture_target = self.camera_config.capture_target.current
        try:
            async with self.camera_lock:
                await self.keep_awake.start()
                self.log("Sequence started")
                self.status.state = SequenceState.PREPARING
                self._touch()
                await self.camera.prepare_for_capture()
                await self.camera.configure_storage(request.storage_destination)
                if request.iso is not None:
                    await self.camera.set_iso(request.iso)
                if request.aperture is not None:
                    await self.camera.set_aperture(request.aperture)

                number = next_sequence_number(output) if output is not None else 1
                for frame in range(1, request.shots + 1):
                    if self._stop_event.is_set():
                        break
                    if output is not None:
                        while any(output.glob(f"light_{number:04d}.*")):
                            number += 1
                        pattern: Path | None = output / f"light_{number:04d}.%C"
                        current_name = self._display_name(number)
                        next_name = self._display_name(number + 1) if frame < request.shots else None
                    else:
                        pattern = None
                        current_name = f"Camera card • frame {frame}"
                        next_name = f"Camera card • frame {frame + 1}" if frame < request.shots else None
                    self.status.current_frame = frame
                    self.status.current_filename = current_name
                    self.status.next_filename = next_name
                    self.status.exposure_elapsed = 0
                    self.status.interval_remaining = 0
                    self.status.state = SequenceState.EXPOSING
                    self._touch()
                    self.log(f"Exposure {frame}/{request.shots} started")

                    result = await self.camera.capture_bulb(
                        request.exposure,
                        pattern,
                        self._stop_event,
                        download=request.storage_destination == StorageDestination.COMPUTER,
                        progress_callback=self._progress,
                        downloading_callback=self._downloading,
                    )
                    if result.saved_name is not None:
                        self.status.last_completed = result.saved_name
                        self.status.sequence_completed += 1
                        if result.path is not None:
                            self.log(f"Downloaded {result.path.name}")
                        else:
                            self.log(f"Saved to camera card: {result.camera_filename}")
                    if result.cancelled or self._stop_event.is_set():
                        break

                    number += 1
                    if frame < request.shots and request.interval > 0:
                        await self._run_interval(request.interval)
                        if self._stop_event.is_set():
                            break

                stopped = self._stop_event.is_set()
                self.status.state = SequenceState.IDLE
                self.status.active = False
                self.status.interval_remaining = 0
                self.status.exposure_elapsed = 0
                self.status.current_filename = None
                self.status.next_filename = None
                self._touch()
                self.log("Sequence stopped safely" if stopped else "Sequence completed")
        except asyncio.CancelledError:
            self.status.state = SequenceState.ERROR
            self.status.active = False
            self.status.error = "Sequence task was cancelled; Bulb close was attempted"
            self._touch()
            self.log(self.status.error, "error")
            raise
        except Exception as exc:
            self.status.state = SequenceState.ERROR
            self.status.active = False
            self.status.error = str(exc)
            self.status.interval_remaining = 0
            self._touch()
            self.log(f"Sequence failed: {exc}", "error")
        finally:
            try:
                await self.camera.restore_capture_target(original_capture_target)
            except Exception as exc:
                self.log(f"Could not restore original camera capture target: {exc}", "warning")
            if self.camera_config.viewfinder.available and self.camera_config.application_mode.available:
                try:
                    await self.camera.set_display_mode(CameraDisplayMode.PLAYBACK)
                    self.camera_config = self.camera.config
                    self.events.publish({"type": "config", "data": self.camera_config.model_dump(mode="json")})
                    self.log("Camera controls/playback restored")
                except Exception as exc:
                    self.log(f"Could not restore camera playback controls: {exc}", "warning")
            await self.keep_awake.stop()

    async def _run_interval(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        self.status.state = SequenceState.INTERVAL
        while True:
            remaining = max(0.0, deadline - loop.time())
            self.status.interval_remaining = remaining
            self._touch()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=min(0.25, remaining))
                self.status.state = SequenceState.STOPPING
                self.status.interval_remaining = 0
                self._touch()
                return
            except asyncio.TimeoutError:
                pass
