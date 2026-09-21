import asyncio

import pytest

from app.camera import SimulatedCamera
from app.models import CameraDisplayMode, SequenceRequest, SequenceState, StorageDestination
from app.sequence import SequenceConflictError, SequenceController, SequenceValidationError, next_sequence_number


async def wait_for_state(controller, state, timeout=2):
    async with asyncio.timeout(timeout):
        while controller.status.state != state:
            await asyncio.sleep(0.005)


def request(**changes):
    values = dict(exposure=0.1, interval=0, shots=1, iso="800", aperture="f/2.8")
    values.update(changes)
    return SequenceRequest(**values)


def test_next_sequence_number_avoids_existing_files(tmp_path):
    (tmp_path / "light_0001.nef").write_bytes(b"one")
    (tmp_path / "light_0007.jpg").write_bytes(b"seven")
    (tmp_path / "unrelated.nef").write_bytes(b"x")
    assert next_sequence_number(tmp_path) == 8


@pytest.mark.asyncio
async def test_sequence_progression_and_unique_filenames(tmp_path):
    camera = SimulatedCamera(time_scale=0.02)
    controller = SequenceController(camera, tmp_path)
    (tmp_path / "light_0001.nef").write_bytes(b"existing")
    await controller.start(request(shots=3, interval=0.02))
    await controller.wait(timeout=2)
    assert controller.status.state == SequenceState.IDLE
    assert controller.status.sequence_completed == 3
    assert [path.name for path in sorted(tmp_path.glob("light_*.nef"))] == [
        "light_0001.nef", "light_0002.nef", "light_0003.nef", "light_0004.nef"
    ]


@pytest.mark.asyncio
async def test_interval_occurs_after_download(tmp_path):
    class RecordingCamera(SimulatedCamera):
        def __init__(self):
            super().__init__(time_scale=1)
            self.starts = []
            self.download_finished = []

        async def start_bulb(self):
            self.starts.append(asyncio.get_running_loop().time())
            await super().start_bulb()

        async def download_capture(self, *args, **kwargs):
            result = await super().download_capture(*args, **kwargs)
            self.download_finished.append(asyncio.get_running_loop().time())
            return result

    camera = RecordingCamera()
    controller = SequenceController(camera, tmp_path)
    await controller.start(request(exposure=0.02, interval=0.06, shots=2))
    await controller.wait(timeout=2)
    assert camera.starts[1] - camera.download_finished[0] >= 0.05


@pytest.mark.asyncio
async def test_stop_during_exposure_closes_bulb_and_returns_idle(tmp_path):
    camera = SimulatedCamera()
    controller = SequenceController(camera, tmp_path)
    await controller.start(request(exposure=10, shots=5))
    await wait_for_state(controller, SequenceState.EXPOSING)
    await controller.stop()
    await controller.wait(timeout=2)
    assert camera.stop_bulb_attempts == 1
    assert controller.status.state == SequenceState.IDLE
    assert controller.status.sequence_completed <= 1


@pytest.mark.asyncio
async def test_stop_during_interval_prevents_next_exposure(tmp_path):
    camera = SimulatedCamera(time_scale=0.1)
    controller = SequenceController(camera, tmp_path)
    await controller.start(request(exposure=0.05, interval=10, shots=3))
    await wait_for_state(controller, SequenceState.INTERVAL)
    attempts = camera.stop_bulb_attempts
    await controller.stop()
    await controller.wait(timeout=2)
    assert controller.status.state == SequenceState.IDLE
    assert camera.stop_bulb_attempts == attempts == 1


@pytest.mark.asyncio
async def test_simulated_download_failure_is_not_marked_complete(tmp_path):
    camera = SimulatedCamera(time_scale=0.01)
    camera.fail_download = True
    controller = SequenceController(camera, tmp_path)
    await controller.start(request())
    await controller.wait(timeout=2)
    assert controller.status.state == SequenceState.ERROR
    assert controller.status.sequence_completed == 0
    assert not list(tmp_path.glob("*.nef"))
    assert camera.stop_bulb_attempts == 1


@pytest.mark.asyncio
async def test_camera_disconnect_blocks_sequence(tmp_path):
    camera = SimulatedCamera()
    controller = SequenceController(camera, tmp_path)
    camera.connected = False
    await controller.refresh_camera()
    with pytest.raises(SequenceValidationError, match="No camera"):
        await controller.start(request())


@pytest.mark.asyncio
async def test_only_one_sequence_can_run(tmp_path):
    controller = SequenceController(SimulatedCamera(), tmp_path)
    await controller.start(request(exposure=10))
    with pytest.raises(SequenceConflictError):
        await controller.start(request())
    await controller.stop()
    await controller.wait(timeout=2)


@pytest.mark.asyncio
async def test_invalid_camera_choice_is_rejected(tmp_path):
    controller = SequenceController(SimulatedCamera(), tmp_path)
    with pytest.raises(SequenceValidationError, match="Unsupported ISO"):
        await controller.start(request(iso="999999"))


@pytest.mark.asyncio
async def test_camera_card_sequence_creates_no_computer_file(tmp_path):
    camera = SimulatedCamera(time_scale=0.01)
    controller = SequenceController(camera, tmp_path)
    await controller.start(request(shots=2, storage_destination=StorageDestination.CAMERA))
    await controller.wait(timeout=2)
    assert controller.status.sequence_completed == 2
    assert controller.status.last_completed == "DSC_0002.NEF"
    assert not list(tmp_path.rglob("*.nef"))
    assert camera.config.capture_target.current == "Memory card"
    assert camera.config.application_mode.current == "Application Mode 1"


@pytest.mark.asyncio
async def test_camera_display_controls_are_serialized(tmp_path):
    camera = SimulatedCamera()
    controller = SequenceController(camera, tmp_path)
    config = await controller.set_camera_display(CameraDisplayMode.LIVEVIEW)
    assert config.viewfinder.current == "1"
    config = await controller.set_camera_display(CameraDisplayMode.PLAYBACK)
    assert config.viewfinder.current == "0"
    assert config.application_mode.current == "Application Mode 1"

    await controller.start(request(exposure=10))
    with pytest.raises(SequenceConflictError):
        await controller.set_camera_display(CameraDisplayMode.LIVEVIEW)
    await controller.stop()
    await controller.wait(timeout=2)
