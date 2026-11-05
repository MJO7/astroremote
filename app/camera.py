from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .models import CameraConfig, CameraDisplayMode, CameraInfo, ConfigProperty, FocusDirection, StorageDestination


class CameraError(RuntimeError):
    pass


class CameraDisconnectedError(CameraError):
    pass


class CameraOwnershipError(CameraError):
    pass


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return "\n".join(value for value in (self.stdout, self.stderr) if value).strip()


@dataclass(slots=True)
class BinaryCommandResult:
    returncode: int
    stdout: bytes
    stderr: str


@dataclass(slots=True)
class CaptureResult:
    path: Path | None
    cancelled: bool
    camera_filename: str | None = None

    @property
    def saved_name(self) -> str | None:
        return self.path.name if self.path is not None else self.camera_filename


ProgressCallback = Callable[[float], Awaitable[None] | None]
StageCallback = Callable[[], Awaitable[None] | None]
DebugCallback = Callable[[str], Awaitable[None] | None]


async def _call(callback, *args) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class GPhotoCamera:
    ISO_CANDIDATES = ("/main/imgsettings/iso",)
    APERTURE_CANDIDATES = (
        "/main/capturesettings/f-number",
        "/main/capturesettings/aperture",
    )
    SHUTTER_CANDIDATES = (
        "/main/capturesettings/shutterspeed",
        "/main/capturesettings/shutter-speed",
    )
    MODE_CANDIDATES = (
        "/main/capturesettings/exposuremode",
        "/main/capturesettings/autoexposuremode",
    )
    BULB_PATH = "/main/actions/bulb"
    CAPTURE_TARGET_CANDIDATES = ("/main/settings/capturetarget",)
    VIEWFINDER_CANDIDATES = ("/main/actions/viewfinder",)
    APPLICATION_MODE_CANDIDATES = ("/main/capturesettings/applicationmode",)
    MANUAL_FOCUS_CANDIDATES = ("/main/actions/manualfocusdrive",)
    FOCUS_MODE_CANDIDATES = ("/main/capturesettings/focusmode2",)
    LIVEVIEW_FOCUS_CANDIDATES = ("/main/capturesettings/liveviewaffocus",)
    FOCUS_AMOUNTS = {1: 100, 2: 600, 3: 2000}
    OWNERSHIP_MARKERS = (
        "Could not claim the USB device",
        "Could not claim interface",
        "USB device is busy",
        "Resource busy",
    )

    def __init__(
        self,
        *,
        executable: str = "gphoto2",
        command_timeout: float = 30,
        debug_callback: DebugCallback | None = None,
    ) -> None:
        self.executable = executable
        self.command_timeout = command_timeout
        self.debug_callback = debug_callback
        self.info = CameraInfo()
        self.config = CameraConfig()
        self._command_lock = asyncio.Lock()
        self.stop_bulb_attempts = 0
        self._liveview_process: asyncio.subprocess.Process | None = None
        self._liveview_reader_task: asyncio.Task[None] | None = None
        self._liveview_stderr_task: asyncio.Task[None] | None = None
        self._liveview_monitor_task: asyncio.Task[None] | None = None
        self._liveview_condition = asyncio.Condition()
        self._liveview_frame: bytes | None = None
        self._liveview_frame_id = 0
        self._liveview_error: str | None = None
        self._liveview_stderr = bytearray()

    @property
    def liveview_running(self) -> bool:
        return self._liveview_process is not None and self._liveview_process.returncode is None

    @property
    def liveview_frame_id(self) -> int:
        return self._liveview_frame_id

    @property
    def liveview_error(self) -> str | None:
        return self._liveview_error

    async def _run(self, *args: str, timeout: float | None = None, check: bool = True) -> CommandResult:
        argv = (self.executable, *args)
        await _call(self.debug_callback, f"$ gphoto2 {' '.join(args)} (started)")
        async with self._command_lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise CameraError("gphoto2 is not installed or not on PATH") from exc
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout or self.command_timeout
                )
            except asyncio.TimeoutError as exc:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise CameraError(
                    f"gphoto2 timed out after {timeout or self.command_timeout:g} seconds "
                    f"while running {' '.join(args)}"
                ) from exc
            except asyncio.CancelledError:
                # A cancelled capture process must not remain alive and retain USB ownership.
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise

        result = CommandResult(
            process.returncode or 0,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )
        await _call(self.debug_callback, f"$ gphoto2 {' '.join(args)}\n{result.combined}".strip())
        if check and result.returncode != 0:
            self._raise_for_result(result)
        return result

    async def _run_binary(self, *args: str, timeout: float | None = None) -> BinaryCommandResult:
        argv = (self.executable, *args)
        async with self._command_lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise CameraError("gphoto2 is not installed or not on PATH") from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout or self.command_timeout
                )
            except asyncio.TimeoutError as exc:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise CameraError("gphoto2 timed out while reading camera media") from exc
            except asyncio.CancelledError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise
        stderr_text = stderr.decode(errors="replace")
        result = BinaryCommandResult(process.returncode or 0, stdout, stderr_text)
        await _call(self.debug_callback, f"$ gphoto2 {' '.join(args)}\n{stderr_text}".strip())
        if result.returncode != 0:
            self._raise_for_result(CommandResult(result.returncode, "", result.stderr))
        return result

    def _raise_for_result(self, result: CommandResult) -> None:
        detail = result.combined or f"gphoto2 exited with status {result.returncode}"
        if any(marker.lower() in detail.lower() for marker in self.OWNERSHIP_MARKERS):
            raise CameraOwnershipError(
                "macOS appears to have claimed the camera's USB/PTP interface. "
                "Release the camera service, then refresh."
            )
        if any(term in detail.lower() for term in ("no camera found", "could not detect", "not found")):
            raise CameraDisconnectedError(detail)
        raise CameraError(detail)

    async def detect(self) -> CameraInfo:
        try:
            result = await self._run("--auto-detect")
        except CameraOwnershipError as exc:
            self.info = CameraInfo(ownership_error=True, message=str(exc))
            return self.info
        except CameraError as exc:
            self.info = CameraInfo(message=str(exc))
            return self.info

        detected: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            match = re.match(r"^(.+?)\s{2,}((?:usb|ptpip|serial):\S+)$", line.strip())
            if match:
                detected.append((match.group(1).strip(), match.group(2).strip()))
        if not detected:
            self.info = CameraInfo(message="No camera detected")
        else:
            name, port = detected[0]
            self.info = CameraInfo(connected=True, name=name, port=port)
        return self.info

    async def list_config(self) -> list[str]:
        result = await self._run("--list-config")
        return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("/")]

    async def get_config(self, path: str) -> ConfigProperty:
        result = await self._run("--get-config", path)
        current: str | None = None
        choices: list[str] = []
        range_values: dict[str, float] = {}
        for line in result.stdout.splitlines():
            if line.startswith("Current:"):
                current = line.partition(":")[2].strip()
            elif line.startswith("Choice:"):
                match = re.match(r"Choice:\s+\d+\s+(.*)$", line)
                if match:
                    choices.append(match.group(1).strip())
            else:
                match = re.match(r"^(Bottom|Top|Step):\s+(-?(?:\d+(?:\.\d*)?|\.\d+))$", line)
                if match:
                    range_values[match.group(1)] = float(match.group(2))
        return ConfigProperty(
            path=path,
            available=True,
            current=current,
            choices=choices,
            minimum=range_values.get("Bottom"),
            maximum=range_values.get("Top"),
            step=range_values.get("Step"),
        )

    @staticmethod
    def _find_config_path(paths: list[str], candidates: tuple[str, ...], hints: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            if candidate in paths:
                return candidate
        for path in paths:
            leaf = path.rsplit("/", 1)[-1].lower().replace("_", "-")
            if any(hint in leaf for hint in hints):
                return path
        return None

    async def get_camera_config(self) -> CameraConfig:
        paths = await self.list_config()

        async def fetch(candidates: tuple[str, ...], hints: tuple[str, ...], label: str) -> ConfigProperty:
            path = self._find_config_path(paths, candidates, hints)
            if not path:
                return ConfigProperty(message=f"Camera does not expose {label}")
            try:
                return await self.get_config(path)
            except CameraError as exc:
                return ConfigProperty(path=path, message=str(exc))

        self.config = CameraConfig(
            iso=await fetch(self.ISO_CANDIDATES, ("iso",), "ISO"),
            aperture=await fetch(self.APERTURE_CANDIDATES, ("f-number", "aperture", "fnumber"), "aperture"),
            shutter_speed=await fetch(self.SHUTTER_CANDIDATES, ("shutterspeed", "shutter-speed"), "shutter speed"),
            exposure_mode=await fetch(self.MODE_CANDIDATES, ("exposuremode", "autoexposuremode"), "exposure mode"),
            capture_target=await fetch(self.CAPTURE_TARGET_CANDIDATES, ("capturetarget",), "capture destination"),
            viewfinder=await fetch(self.VIEWFINDER_CANDIDATES, ("viewfinder",), "Live View"),
            application_mode=await fetch(self.APPLICATION_MODE_CANDIDATES, ("applicationmode",), "camera playback mode"),
            manual_focus=await fetch(self.MANUAL_FOCUS_CANDIDATES, ("manualfocusdrive",), "manual focus drive"),
            focus_mode=await fetch(self.FOCUS_MODE_CANDIDATES, ("focusmode2",), "remote focus mode"),
            liveview_focus=await fetch(self.LIVEVIEW_FOCUS_CANDIDATES, ("liveviewaffocus",), "Live View focus mode"),
        )
        return self.config

    async def set_config(self, path: str, value: str) -> None:
        await self._run("--set-config", f"{path}={value}")

    async def set_iso(self, value: str) -> None:
        if not self.config.iso.available or not self.config.iso.path:
            raise CameraError("ISO is not exposed by the camera")
        await self.set_config(self.config.iso.path, value)

    async def set_aperture(self, value: str) -> None:
        if not self.config.aperture.available or not self.config.aperture.path:
            raise CameraError("Aperture is not exposed by the camera")
        await self.set_config(self.config.aperture.path, value)

    @staticmethod
    def _choice_matching(prop: ConfigProperty, *needles: str) -> str | None:
        for choice in prop.choices:
            normalized = choice.casefold()
            if any(needle.casefold() in normalized for needle in needles):
                return choice
        return None

    def storage_choice(self, destination: StorageDestination) -> str | None:
        prop = self.config.capture_target
        # Nikon's proven tethered capture path stages the frame on the card
        # before gphoto2 downloads it to the Mac. Internal RAM did not trigger
        # a shutter release on the D3500 used for physical verification.
        return self._choice_matching(prop, "memory card", "card")

    async def configure_storage(self, destination: StorageDestination) -> None:
        prop = self.config.capture_target
        choice = self.storage_choice(destination)
        if not prop.available or not prop.path or choice is None:
            label = "computer" if destination == StorageDestination.COMPUTER else "camera memory card"
            raise CameraError(f"The camera does not expose a supported capture target for {label}")
        await self.set_config(prop.path, choice)
        prop.current = choice

    async def restore_capture_target(self, value: str | None) -> None:
        prop = self.config.capture_target
        if value and prop.available and prop.path and value in prop.choices:
            await self.set_config(prop.path, value)
            prop.current = value

    async def set_display_mode(self, mode: CameraDisplayMode) -> None:
        viewfinder = self.config.viewfinder
        application = self.config.application_mode
        if not viewfinder.available or not viewfinder.path:
            raise CameraError("This camera does not expose a Live View control through gphoto2")

        if mode == CameraDisplayMode.LIVEVIEW:
            await self.start_liveview()
            return

        await self.stop_liveview()
        if not application.available or not application.path:
            raise CameraError(
                "Live View was stopped, but this camera does not expose the application mode needed for playback"
            )
        playback = self._choice_matching(application, "mode 1")
        if playback is None:
            raise CameraError("The camera did not report a playback/application mode choice")
        await self.set_config(application.path, playback)
        application.current = playback

    async def start_liveview(self, *, focus_amount: int | None = None) -> None:
        """Start one persistent, untranscoded preview stream from the camera."""
        if self.liveview_running:
            return
        await self.stop_liveview(send_off=False)
        viewfinder = self.config.viewfinder
        application = self.config.application_mode
        if not viewfinder.available or not viewfinder.path:
            raise CameraError("This camera does not expose a Live View control through gphoto2")
        if application.available and application.path:
            remote = self._choice_matching(application, "mode 0")
            if remote:
                await self.set_config(application.path, remote)
                application.current = remote

        # capture-movie continuously emits concatenated JPEG previews on stdout.
        # Keeping only the newest decoded boundary prevents slow browser clients
        # from adding latency or unbounded memory use.
        args: tuple[str, ...]
        if focus_amount is None:
            args = ("--stdout", "--capture-movie")
        else:
            manual_focus = self.config.manual_focus
            if not manual_focus.available or not manual_focus.path:
                raise CameraError("This camera does not expose electronic manual focus control")
            focus_args: list[str] = ["--set-config", f"{viewfinder.path}=1"]
            focus_mode = self.config.focus_mode
            if focus_mode.available and focus_mode.path:
                manual_choice = self._choice_matching(focus_mode, "mf (selection)", "manual")
                if manual_choice:
                    focus_args.extend(("--set-config", f"{focus_mode.path}={manual_choice}"))
                    focus_mode.current = manual_choice
            liveview_focus = self.config.liveview_focus
            if liveview_focus.available and liveview_focus.path:
                single_choice = self._choice_matching(liveview_focus, "single-servo")
                if single_choice:
                    focus_args.extend(("--set-config", f"{liveview_focus.path}={single_choice}"))
                    liveview_focus.current = single_choice
            focus_args.extend(
                ("--set-config", f"{manual_focus.path}={focus_amount}", "--stdout", "--capture-movie")
            )
            args = tuple(focus_args)
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CameraError("gphoto2 is not installed or not on PATH") from exc
        self._liveview_process = process
        self._liveview_frame = None
        self._liveview_error = None
        self._liveview_stderr.clear()
        assert process.stdout is not None and process.stderr is not None
        self._liveview_reader_task = asyncio.create_task(
            self._read_liveview_frames(process.stdout), name="astroremote-liveview-frames"
        )
        self._liveview_stderr_task = asyncio.create_task(
            self._read_liveview_stderr(process.stderr), name="astroremote-liveview-stderr"
        )
        self._liveview_monitor_task = asyncio.create_task(
            self._monitor_liveview(process), name="astroremote-liveview-monitor"
        )
        try:
            await self.next_liveview_frame(self._liveview_frame_id, timeout=10)
        except (CameraError, asyncio.TimeoutError) as exc:
            await self.stop_liveview(send_off=False)
            if isinstance(exc, asyncio.TimeoutError):
                raise CameraError("Timed out waiting for the first Live View frame") from exc
            raise
        viewfinder.current = "1"
        await _call(self.debug_callback, f"$ gphoto2 {' '.join(args)}\nLive View JPEG stream active")

    async def adjust_focus(self, direction: FocusDirection, step: int) -> int:
        """Apply one relative electronic-focus pulse and restore the preview stream."""
        manual_focus = self.config.manual_focus
        if not manual_focus.available or not manual_focus.path:
            raise CameraError("The connected camera does not expose electronic manual focus")
        if manual_focus.minimum is None or manual_focus.maximum is None:
            raise CameraError("The camera's focus control is not a Nikon relative-step drive")
        if not self.liveview_running:
            raise CameraError("Start Live View before adjusting focus")
        magnitude = self.FOCUS_AMOUNTS.get(step)
        if magnitude is None:
            raise CameraError("Focus step must be fine, medium, or coarse")
        amount = -magnitude if direction == FocusDirection.NEAR else magnitude
        if manual_focus.minimum is not None and amount < manual_focus.minimum:
            raise CameraError("Requested focus pulse is below the camera's reported range")
        if manual_focus.maximum is not None and amount > manual_focus.maximum:
            raise CameraError("Requested focus pulse is above the camera's reported range")

        # capture-movie owns the USB/PTP session. Restart it with the focus
        # command in the same gphoto2 invocation so Nikon still sees Live View
        # when the relative lens-drive command is issued.
        await self.stop_liveview(send_off=False)
        try:
            await self.start_liveview(focus_amount=amount)
        except CameraError as focus_error:
            try:
                await self.start_liveview()
            except CameraError:
                pass
            raise CameraError(f"Focus adjustment failed: {focus_error}") from focus_error
        return amount

    async def _read_liveview_frames(self, stream: asyncio.StreamReader) -> None:
        buffer = bytearray()
        while chunk := await stream.read(64 * 1024):
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    if len(buffer) > 16 * 1024 * 1024:
                        buffer.clear()
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                async with self._liveview_condition:
                    self._liveview_frame = frame
                    self._liveview_frame_id += 1
                    self._liveview_condition.notify_all()

    async def _read_liveview_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            self._liveview_stderr.extend(chunk)
            if len(self._liveview_stderr) > 64 * 1024:
                del self._liveview_stderr[: len(self._liveview_stderr) - 64 * 1024]

    async def _monitor_liveview(self, process: asyncio.subprocess.Process) -> None:
        returncode = await process.wait()
        if process is self._liveview_process and returncode != 0:
            detail = self._liveview_stderr.decode(errors="replace").strip()
            self._liveview_error = detail or f"gphoto2 Live View exited with status {returncode}"
        async with self._liveview_condition:
            self._liveview_condition.notify_all()

    async def next_liveview_frame(
        self, after_frame_id: int, *, timeout: float = 15
    ) -> tuple[int, bytes, str]:
        async with self._liveview_condition:
            await asyncio.wait_for(
                self._liveview_condition.wait_for(
                    lambda: self._liveview_frame_id > after_frame_id or not self.liveview_running
                ),
                timeout=timeout,
            )
            if self._liveview_frame_id > after_frame_id and self._liveview_frame is not None:
                return self._liveview_frame_id, self._liveview_frame, "image/jpeg"
            raise CameraError(self._liveview_error or "Live View stream stopped")

    async def stop_liveview(self, *, send_off: bool = True) -> None:
        process = self._liveview_process
        self._liveview_process = None
        if process is not None:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            tasks = tuple(
                task
                for task in (
                    self._liveview_reader_task,
                    self._liveview_stderr_task,
                    self._liveview_monitor_task,
                )
                if task is not None
            )
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
                except asyncio.TimeoutError:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            await _call(
                self.debug_callback,
                f"Live View process stopped\n{self._liveview_stderr.decode(errors='replace').strip()}".strip(),
            )
        self._liveview_reader_task = None
        self._liveview_stderr_task = None
        self._liveview_monitor_task = None
        async with self._liveview_condition:
            self._liveview_frame = None
            self._liveview_condition.notify_all()
        viewfinder = self.config.viewfinder
        if send_off and viewfinder.available and viewfinder.path:
            await self.set_config(viewfinder.path, "0")
        if viewfinder.available:
            viewfinder.current = "0"

    async def prepare_for_capture(self) -> None:
        """Leave camera-side Live View/playback mode before remote Bulb control."""
        await self.stop_liveview()
        application = self.config.application_mode
        if application.available and application.path:
            remote = self._choice_matching(application, "mode 0")
            if remote:
                await self.set_config(application.path, remote)
                application.current = remote

    async def prepare_for_gallery(self) -> None:
        """Release Live View and select remote mode for read-only card access."""
        await self.stop_liveview()
        application = self.config.application_mode
        if application.available and application.path:
            remote = self._choice_matching(application, "mode 0")
            if remote:
                await self.set_config(application.path, remote)
                application.current = remote

    async def list_camera_files(self) -> list[str]:
        result = await self._run("--quiet", "--recurse", "--list-files", timeout=60)
        paths = []
        for line in result.stdout.splitlines():
            path = line.strip()
            if path.startswith("/") and "\x00" not in path:
                paths.append(path)
        return paths

    async def get_camera_thumbnail(self, camera_path: str) -> tuple[bytes, str]:
        if not camera_path.startswith("/") or any(ord(char) < 32 for char in camera_path):
            raise CameraError("Invalid camera file path")
        result = await self._run_binary("--stdout", f"--get-thumbnail={camera_path}", timeout=45)
        if not result.stdout:
            raise CameraError(f"The camera did not return a preview for {Path(camera_path).name}")
        if result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            content_type = "image/png"
        elif result.stdout.lstrip().startswith(b"<svg"):
            content_type = "image/svg+xml"
        else:
            content_type = "image/jpeg"
        return result.stdout, content_type

    async def start_bulb(self) -> None:
        await self.set_config(self.BULB_PATH, "1")

    async def stop_bulb(self) -> None:
        self.stop_bulb_attempts += 1
        await self.set_config(self.BULB_PATH, "0")

    async def download_capture(self, filename_pattern: Path, *, required: bool = True) -> Path | None:
        before = set(filename_pattern.parent.glob(f"{filename_pattern.stem}.*"))
        result = await self._run(
            "--filename",
            str(filename_pattern),
            "--wait-event-and-download=10s",
            timeout=20,
            check=False,
        )
        after = set(filename_pattern.parent.glob(f"{filename_pattern.stem}.*"))
        created = sorted(path for path in after - before if path.is_file())
        if result.returncode != 0:
            if not required and not created:
                return None
            self._raise_for_result(result)
        if not created:
            if required:
                raise CameraError("gphoto2 did not download an image file")
            return None
        return created[0]

    async def capture_bulb(
        self,
        duration: float,
        filename_pattern: Path | None,
        cancellation_event: asyncio.Event,
        *,
        download: bool = True,
        progress_callback: ProgressCallback | None = None,
        downloading_callback: StageCallback | None = None,
    ) -> CaptureResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        primary_error: BaseException | None = None
        cancelled = False
        download_announced = False
        combined_succeeded = False
        capture_task: asyncio.Task[CommandResult] | None = None
        if download and filename_pattern is None:
            raise CameraError("A computer filename is required when downloading captures")
        if duration < 1:
            raise CameraError("Nikon Bulb exposures must be at least 1 second")
        card_files_before = set(await self.list_camera_files()) if not download else set()
        prefix = filename_pattern.stem if filename_pattern is not None else None
        before = set(filename_pattern.parent.glob(f"{prefix}.*")) if filename_pattern is not None else set()
        completion_args: tuple[str, ...]
        if download:
            assert filename_pattern is not None
            completion_args = (
                # A seconds-suffixed wait runs for the full period even after
                # the file arrives. Exit as soon as the image is downloaded.
                "--wait-event-and-download=FILEADDED",
            )
        else:
            completion_args = ("--wait-event=10s",)
        try:
            # Keep one gphoto2 process attached for the normal open/wait/close/download
            # path. This mirrors the proven Nikon workflow and reduces USB ownership
            # hand-offs. Cancellation terminates it, then the finally block issues an
            # independent emergency bulb-off command.
            capture_task = asyncio.create_task(
                self._run(
                    *(("--filename", str(filename_pattern)) if download and filename_pattern is not None else ()),
                    "--set-config",
                    f"{self.BULB_PATH}=1",
                    (
                        f"--wait-event={duration:g}s"
                        if float(duration).is_integer()
                        else f"--wait-event={round(duration * 1000)}ms"
                    ),
                    "--set-config",
                    f"{self.BULB_PATH}=0",
                    *completion_args,
                    timeout=duration + 15,
                    check=False,
                )
            )
            while not capture_task.done():
                elapsed = min(loop.time() - started, duration)
                await _call(progress_callback, elapsed)
                if elapsed >= duration and not download_announced:
                    download_announced = True
                    await _call(downloading_callback)
                try:
                    await asyncio.wait_for(cancellation_event.wait(), timeout=0.25)
                    cancelled = True
                    if elapsed >= duration:
                        # The shutter-close/download command may still be in
                        # flight. Let it finish so STOP does not discard the
                        # image or suppress gphoto2's diagnostic output.
                        await asyncio.wait({capture_task}, timeout=0.25)
                        continue
                    capture_task.cancel()
                    try:
                        await capture_task
                    except asyncio.CancelledError:
                        pass
                    break
                except asyncio.TimeoutError:
                    pass
            result = None if capture_task.cancelled() else await capture_task
            if result is not None and result.returncode != 0:
                self._raise_for_result(result)
            combined_succeeded = result is not None
        except BaseException as exc:
            if capture_task is not None and not capture_task.done():
                capture_task.cancel()
                try:
                    await capture_task
                except asyncio.CancelledError:
                    pass
            primary_error = exc
        finally:
            # The combined command already closed Bulb on success. Only issue
            # an independent close if it failed or was cancelled mid-exposure.
            if not combined_succeeded:
                try:
                    await asyncio.shield(self.stop_bulb())
                except BaseException as stop_exc:
                    if primary_error is None:
                        primary_error = stop_exc

        if primary_error is not None:
            raise primary_error
        if not download_announced:
            await _call(downloading_callback)
        created = []
        if filename_pattern is not None and prefix is not None:
            created = sorted(
                path for path in set(filename_pattern.parent.glob(f"{prefix}.*")) - before if path.is_file()
            )
        path = created[0] if created else None
        camera_filename = self._camera_filename_from_output(result.combined if result is not None else "")
        if cancelled and download and path is None:
            # STOP still gives the camera a chance to deliver the shortened exposure.
            assert filename_pattern is not None
            path = await self.download_capture(filename_pattern, required=False)
        if not cancelled and download and path is None:
            raise CameraError("gphoto2 completed without downloading an image file")
        if not cancelled and not download and camera_filename is None:
            camera_filename = await self._confirm_card_capture(
                card_files_before, cancellation_event, timeout=max(30.0, duration + 30.0)
            )
            cancelled = cancellation_event.is_set()
            if camera_filename is None and not cancelled:
                raise CameraError(
                    "Could not confirm a new image on the camera memory card after waiting "
                    "for processing. Check camera playback, card capacity/write protection, "
                    "and Long exposure NR before retrying."
                )
        return CaptureResult(path=path, cancelled=cancelled, camera_filename=camera_filename)

    async def _confirm_card_capture(
        self, before: set[str], cancellation_event: asyncio.Event, *, timeout: float
    ) -> str | None:
        """Recover a delayed/missed FILEADDED event without counting existing photos."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not cancellation_event.is_set():
            added = sorted(set(await self.list_camera_files()) - before)
            if added:
                return Path(added[0]).name
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            # Short waits keep STOP responsive while allowing camera-side processing.
            result = await self._run(
                "--wait-event=1s", timeout=10, check=True
            )
            filename = self._camera_filename_from_output(result.combined)
            if filename:
                return filename
        return None

    @staticmethod
    def _camera_filename_from_output(output: str) -> str | None:
        for line in output.splitlines():
            match = re.search(r"(?:FILEADDED|File added:)\s+(.+)$", line, flags=re.IGNORECASE)
            if match:
                return Path(match.group(1).strip()).name
        return None


class SimulatedCamera(GPhotoCamera):
    def __init__(self, *, time_scale: float = 1.0, debug_callback: DebugCallback | None = None) -> None:
        super().__init__(debug_callback=debug_callback)
        self.time_scale = time_scale
        self.connected = True
        self.fail_download = False
        self.fail_start = False
        self._camera_file_number = 1
        self._sim_liveview_running = False
        self._sim_camera_files = [
            f"/store_00010001/DCIM/100D3500/DSC_{number:04d}.NEF"
            for number in range(9001, 9019)
        ]
        self.info = CameraInfo(
            connected=True,
            name="Nikon DSC D3500 (Simulated)",
            port="simulated:001",
            simulation=True,
        )
        self.config = CameraConfig(
            iso=ConfigProperty(path="/main/imgsettings/iso", available=True, current="800", choices=["100", "200", "400", "800", "1600", "3200"]),
            aperture=ConfigProperty(path="/main/capturesettings/f-number", available=True, current="f/2.8", choices=["f/1.8", "f/2.8", "f/4", "f/5.6", "f/8"]),
            shutter_speed=ConfigProperty(message="Bulb duration is controlled by AstroRemote"),
            exposure_mode=ConfigProperty(path="/main/capturesettings/exposuremode", available=True, current="M", choices=["M"]),
            capture_target=ConfigProperty(path="/main/settings/capturetarget", available=True, current="Memory card", choices=["Internal RAM", "Memory card"]),
            viewfinder=ConfigProperty(path="/main/actions/viewfinder", available=True, current="0"),
            application_mode=ConfigProperty(path="/main/capturesettings/applicationmode", available=True, current="Application Mode 0", choices=["Application Mode 0", "Application Mode 1"]),
            manual_focus=ConfigProperty(path="/main/actions/manualfocusdrive", available=True, current="0", minimum=-32767, maximum=32767, step=1),
            focus_mode=ConfigProperty(path="/main/capturesettings/focusmode2", available=True, current="AF-A", choices=["AF-S", "AF-C", "AF-A", "MF (fixed)", "MF (selection)"]),
            liveview_focus=ConfigProperty(path="/main/capturesettings/liveviewaffocus", available=True, current="Single-servo AF", choices=["Single-servo AF", "Full-time-servo AF", "Manual Focus"]),
        )
        self._sim_focus_position = 0

    async def detect(self) -> CameraInfo:
        if not self.connected:
            self.info = CameraInfo(simulation=True, message="Simulated camera disconnected")
        else:
            self.info = CameraInfo(connected=True, name="Nikon DSC D3500 (Simulated)", port="simulated:001", simulation=True)
        return self.info

    async def get_camera_config(self) -> CameraConfig:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        return self.config

    async def set_iso(self, value: str) -> None:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        if value not in self.config.iso.choices:
            raise CameraError("Unsupported simulated ISO")
        self.config.iso.current = value

    async def set_aperture(self, value: str) -> None:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        if value not in self.config.aperture.choices:
            raise CameraError("Unsupported simulated aperture")
        self.config.aperture.current = value

    async def configure_storage(self, destination: StorageDestination) -> None:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        self.config.capture_target.current = "Memory card"

    async def restore_capture_target(self, value: str | None) -> None:
        if value:
            self.config.capture_target.current = value

    async def set_display_mode(self, mode: CameraDisplayMode) -> None:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        if mode == CameraDisplayMode.LIVEVIEW:
            await self.start_liveview()
        else:
            await self.stop_liveview()
            self.config.application_mode.current = "Application Mode 1"

    @property
    def liveview_running(self) -> bool:
        return self._sim_liveview_running

    async def start_liveview(self) -> None:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        self._sim_liveview_running = True
        self.config.application_mode.current = "Application Mode 0"
        self.config.viewfinder.current = "1"

    async def stop_liveview(self, *, send_off: bool = True) -> None:
        self._sim_liveview_running = False
        self.config.viewfinder.current = "0"

    async def adjust_focus(self, direction: FocusDirection, step: int) -> int:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        if not self.liveview_running:
            raise CameraError("Start Live View before adjusting focus")
        magnitude = self.FOCUS_AMOUNTS.get(step)
        if magnitude is None:
            raise CameraError("Focus step must be fine, medium, or coarse")
        amount = -magnitude if direction == FocusDirection.NEAR else magnitude
        self._sim_focus_position = max(-32767, min(32767, self._sim_focus_position + amount))
        await asyncio.sleep(0.03 * self.time_scale)
        return amount

    async def next_liveview_frame(
        self, after_frame_id: int, *, timeout: float = 15
    ) -> tuple[int, bytes, str]:
        if not self.liveview_running:
            raise CameraError("Live View stream stopped")
        await asyncio.sleep(0.08)
        self._liveview_frame_id = max(self._liveview_frame_id + 1, after_frame_id + 1)
        phase = self._liveview_frame_id % 120
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="640" viewBox="0 0 960 640">
<rect width="960" height="640" fill="#05080d"/><circle cx="{420 + phase}" cy="285" r="115" fill="#17243a" stroke="#4f9cff" stroke-width="3"/>
<circle cx="{470 + phase}" cy="250" r="24" fill="#b9dbff"/><g fill="#fff" opacity=".75"><circle cx="120" cy="100" r="2"/><circle cx="780" cy="160" r="2"/><circle cx="220" cy="520" r="2"/><circle cx="850" cy="460" r="2"/></g>
<text x="36" y="590" fill="#83baff" font-family="system-ui" font-size="24">SIMULATED LIVE VIEW</text></svg>'''
        return self._liveview_frame_id, svg.encode(), "image/svg+xml"

    async def prepare_for_capture(self) -> None:
        self.config.viewfinder.current = "0"
        self.config.application_mode.current = "Application Mode 0"

    async def prepare_for_gallery(self) -> None:
        await self.stop_liveview()
        self.config.application_mode.current = "Application Mode 0"

    async def list_camera_files(self) -> list[str]:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        await asyncio.sleep(0.03 * self.time_scale)
        return list(self._sim_camera_files)

    async def get_camera_thumbnail(self, camera_path: str) -> tuple[bytes, str]:
        if camera_path not in self._sim_camera_files:
            raise CameraError("Simulated camera file not found")
        number_match = re.search(r"(\d+)(?=\.[^.]+$)", camera_path)
        number = int(number_match.group(1)) if number_match else 0
        hue = 205 + number * 7 % 80
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="720" height="480" viewBox="0 0 720 480">
<defs><radialGradient id="g"><stop stop-color="hsl({hue} 55% 32%)"/><stop offset="1" stop-color="#02050a"/></radialGradient></defs>
<rect width="720" height="480" fill="url(#g)"/><circle cx="{180 + number * 17 % 360}" cy="{120 + number * 23 % 230}" r="{30 + number % 5 * 12}" fill="#d7eaff" opacity=".78"/>
<g fill="#fff" opacity=".72"><circle cx="80" cy="70" r="2"/><circle cx="630" cy="85" r="3"/><circle cx="580" cy="370" r="2"/><circle cx="130" cy="390" r="2"/></g>
<text x="24" y="448" fill="#c9ddf7" font-family="system-ui" font-size="22">DSC_{number:04d}.NEF</text></svg>'''
        return svg.encode(), "image/svg+xml"

    async def start_bulb(self) -> None:
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        if self.fail_start:
            raise CameraError("Simulated bulb start failure")

    async def stop_bulb(self) -> None:
        self.stop_bulb_attempts += 1

    async def download_capture(self, filename_pattern: Path, *, required: bool = True) -> Path | None:
        if self.fail_download:
            raise CameraError("Simulated download failure")
        if not self.connected:
            raise CameraDisconnectedError("Simulated camera disconnected")
        await asyncio.sleep(0.02 * self.time_scale)
        output = filename_pattern.with_suffix(".nef")
        if output.exists():
            raise CameraError(f"Refusing to overwrite existing file: {output.name}")
        output.write_bytes(b"ASTROREMOTE SIMULATED NEF\n")
        await _call(self.debug_callback, f"Simulated download: {output}")
        return output

    async def capture_bulb(
        self,
        duration: float,
        filename_pattern: Path | None,
        cancellation_event: asyncio.Event,
        *,
        download: bool = True,
        progress_callback: ProgressCallback | None = None,
        downloading_callback: StageCallback | None = None,
    ) -> CaptureResult:
        # Scale wall-clock time in tests while reporting real requested exposure progress.
        scaled_duration = duration * self.time_scale
        loop = asyncio.get_running_loop()
        started = loop.time()
        primary_error: BaseException | None = None
        cancelled = False
        try:
            await self.start_bulb()
            while True:
                wall_elapsed = loop.time() - started
                elapsed = min(wall_elapsed / max(self.time_scale, 1e-9), duration)
                await _call(progress_callback, elapsed)
                remaining = scaled_duration - wall_elapsed
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(cancellation_event.wait(), timeout=min(0.05, remaining))
                    cancelled = True
                    break
                except asyncio.TimeoutError:
                    pass
        except BaseException as exc:
            primary_error = exc
        finally:
            try:
                await asyncio.shield(self.stop_bulb())
            except BaseException as stop_exc:
                if primary_error is None:
                    primary_error = stop_exc
        if primary_error:
            raise primary_error
        await _call(downloading_callback)
        if download:
            if filename_pattern is None:
                raise CameraError("A simulated computer filename is required")
            path = await self.download_capture(filename_pattern, required=not cancelled)
            return CaptureResult(path=path, cancelled=cancelled)
        filename = f"DSC_{self._camera_file_number:04d}.NEF"
        self._sim_camera_files.append(f"/store_00010001/DCIM/100D3500/{filename}")
        self._camera_file_number += 1
        await _call(self.debug_callback, f"Simulated save to camera card: {filename}")
        return CaptureResult(path=None, cancelled=cancelled, camera_filename=filename)
