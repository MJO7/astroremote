import asyncio

import pytest

from app.camera import BinaryCommandResult, CameraError, CommandResult, GPhotoCamera
from app.models import CameraConfig, ConfigProperty, StorageDestination


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class PersistentFakeProcess:
    def __init__(self):
        self.returncode = None
        self._finished = asyncio.Event()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(b"noise\xff\xd8jpeg-preview\xff\xd9")

    async def wait(self):
        await self._finished.wait()
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._finished.set()

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._finished.set()


@pytest.mark.asyncio
async def test_subprocess_is_invoked_as_argument_array(monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess(stdout=b"Model                          Port\n----------------------------------------------------------\nNikon DSC D3500                usb:001,002\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    camera = GPhotoCamera()
    info = await camera.detect()
    assert info.connected
    assert calls == [("gphoto2", "--auto-detect")]


@pytest.mark.asyncio
async def test_bulb_off_attempted_when_start_fails(monkeypatch, tmp_path):
    camera = GPhotoCamera()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        raise CameraError("start failed after camera command")

    async def fake_set(path, value):
        calls.append((path, value))

    monkeypatch.setattr(camera, "_run", fake_run)
    monkeypatch.setattr(camera, "set_config", fake_set)
    with pytest.raises(CameraError):
        await camera.capture_bulb(1, tmp_path / "light_0001.%C", asyncio.Event())
    assert calls[-1] == (camera.BULB_PATH, "0")


@pytest.mark.asyncio
async def test_unknown_ptp_warning_does_not_override_success(monkeypatch):
    warning = b'UNKNOWN PTP Property 0000d1a4 changed, "liveviewprohibit"\n'

    async def fake_exec(*args, **kwargs):
        return FakeProcess(stderr=warning, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await GPhotoCamera()._run("--auto-detect")
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_physical_capture_uses_proven_combined_gphoto_workflow(monkeypatch, tmp_path):
    camera = GPhotoCamera()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        if "--wait-event-and-download=10s" in args:
            (tmp_path / "light_0001.nef").write_bytes(b"nef")
        return CommandResult(0, "", "")

    monkeypatch.setattr(camera, "_run", fake_run)
    result = await camera.capture_bulb(2, tmp_path / "light_0001.%C", asyncio.Event())
    assert result.path == tmp_path / "light_0001.nef"
    combined = calls[0]
    assert f"{camera.BULB_PATH}=1" in combined
    assert "--wait-event=2s" in combined
    assert f"{camera.BULB_PATH}=0" in combined
    assert "--wait-event-and-download=10s" in combined
    assert calls[-1] == ("--set-config", f"{camera.BULB_PATH}=0")


@pytest.mark.asyncio
async def test_camera_card_capture_requires_file_added_event(monkeypatch):
    camera = GPhotoCamera()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        output = "FILEADDED /store_00010001/DCIM/100D3500/DSC_0042.NEF" if "--wait-event=10s" in args else ""
        return CommandResult(0, output, "")

    monkeypatch.setattr(camera, "_run", fake_run)
    result = await camera.capture_bulb(1, None, asyncio.Event(), download=False)
    assert result.camera_filename == "DSC_0042.NEF"
    assert "--wait-event=10s" in calls[0]
    assert "--wait-event-and-download=10s" not in calls[0]


def test_capture_target_choices_are_camera_reported():
    camera = GPhotoCamera()
    camera.config = CameraConfig(
        capture_target=ConfigProperty(
            path="/main/settings/capturetarget",
            available=True,
            current="Memory card",
            choices=["Internal RAM", "Memory card"],
        )
    )
    assert camera.storage_choice(StorageDestination.COMPUTER) == "Internal RAM"
    assert camera.storage_choice(StorageDestination.CAMERA) == "Memory card"


@pytest.mark.asyncio
async def test_liveview_process_stays_attached_until_stopped(monkeypatch):
    camera = GPhotoCamera()
    camera.config = CameraConfig(
        viewfinder=ConfigProperty(path="/main/actions/viewfinder", available=True, current="0"),
        application_mode=ConfigProperty(
            path="/main/capturesettings/applicationmode",
            available=True,
            current="Application Mode 1",
            choices=["Application Mode 0", "Application Mode 1"],
        ),
    )
    set_calls = []
    process = PersistentFakeProcess()
    spawn_calls = []

    async def fake_set(path, value):
        set_calls.append((path, value))

    async def fake_exec(*args, **kwargs):
        spawn_calls.append(args)
        return process

    monkeypatch.setattr(camera, "set_config", fake_set)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await camera.start_liveview()
    assert camera.liveview_running
    assert "--capture-movie" in spawn_calls[0]
    assert "--stdout" in spawn_calls[0]
    assert set_calls[0][-1] == "Application Mode 0"
    frame_id, frame, content_type = await camera.next_liveview_frame(0)
    assert frame_id == 1
    assert frame == b"\xff\xd8jpeg-preview\xff\xd9"
    assert content_type == "image/jpeg"

    await camera.stop_liveview()
    assert not camera.liveview_running
    assert set_calls[-1] == ("/main/actions/viewfinder", "0")


@pytest.mark.asyncio
async def test_gallery_lists_full_card_and_reads_thumbnail_without_shell(monkeypatch):
    camera = GPhotoCamera()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return CommandResult(
            0,
            "/store_00010001/DCIM/100D3500/DSC_0001.NEF\n"
            "/store_00010001/DCIM/100D3500/DSC_0002.JPG\n",
            "",
        )

    async def fake_binary(*args, **kwargs):
        calls.append(args)
        return BinaryCommandResult(0, b"\xff\xd8thumbnail\xff\xd9", "")

    monkeypatch.setattr(camera, "_run", fake_run)
    monkeypatch.setattr(camera, "_run_binary", fake_binary)
    paths = await camera.list_camera_files()
    assert paths[-1].endswith("DSC_0002.JPG")
    assert calls[0] == ("--quiet", "--recurse", "--list-files")

    content, content_type = await camera.get_camera_thumbnail(paths[0])
    assert content == b"\xff\xd8thumbnail\xff\xd9"
    assert content_type == "image/jpeg"
    assert calls[1] == ("--stdout", f"--get-thumbnail={paths[0]}")


@pytest.mark.asyncio
async def test_gallery_rejects_invalid_camera_path():
    with pytest.raises(CameraError, match="Invalid camera file path"):
        await GPhotoCamera().get_camera_thumbnail("../../etc/passwd")
