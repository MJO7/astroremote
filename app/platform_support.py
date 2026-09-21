from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path


class KeepAwake:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if platform.system() != "Darwin" or self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                "caffeinate",
                "-dims",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            self._process = None

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


class CameraOwnershipHelper:
    """Optional, narrowly scoped executable used to release macOS PTP services."""

    def __init__(self, helper_path: str | None = None) -> None:
        self.helper_path = helper_path or os.getenv("ASTROREMOTE_PTP_HELPER")

    @property
    def configured(self) -> bool:
        return bool(self.helper_path)

    async def release(self) -> str:
        if platform.system() != "Darwin":
            return "Camera ownership helper is only needed on macOS."
        if not self.helper_path:
            raise RuntimeError("ASTROREMOTE_PTP_HELPER is not configured")
        helper = Path(self.helper_path)
        if not helper.is_absolute() or not helper.is_file() or not os.access(helper, os.X_OK):
            raise RuntimeError("ASTROREMOTE_PTP_HELPER must be an executable absolute path")
        process = await asyncio.create_subprocess_exec(
            str(helper),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        detail = (stdout + stderr).decode(errors="replace").strip()
        if process.returncode != 0:
            raise RuntimeError(detail or f"PTP helper exited with status {process.returncode}")
        return detail or "macOS camera services released"
