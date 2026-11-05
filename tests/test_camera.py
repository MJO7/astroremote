import asyncio

import pytest

from app.camera import BinaryCommandResult, CameraError, CommandResult, GPhotoCamera
from app.models import CameraConfig, ConfigProperty, FocusDirection, StorageDestination


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
async def test_range_config_includes_camera_reported_bounds(monkeypatch):
    camera = GPhotoCamera()

    async def fake_run(*args, **kwargs):
        return CommandResult(
            0,
            "Type: RANGE\nCurrent: 0\nBottom: -32767\nTop: 32767\nStep: 1\n",
            "",
        )

    monkeypatch.setattr(camera, "_run", fake_run)
    prop = await camera.get_config("/main/actions/manualfocusdrive")
    assert prop.minimum == -32767
    assert prop.maximum == 32767
    assert prop.step == 1


@pytest.mark.asyncio
async def test_physical_capture_uses_proven_combined_gphoto_workflow(monkeypatch, tmp_path):
    camera = GPhotoCamera()
    calls = []
    options = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        options.append(kwargs)
        if "--wait-event-and-download=FILEADDED" in args:
            (tmp_path / "light_0001.nef").write_bytes(b"nef")
        return CommandResult(0, "", "")

    monkeypatch.setattr(camera, "_run", fake_run)
    result = await camera.capture_bulb(2, tmp_path / "light_0001.%C", asyncio.Event())
    assert result.path == tmp_path / "light_0001.nef"
    combined = calls[0]
    assert f"{camera.BULB_PATH}=1" in combined
    assert "--wait-event=2s" in combined
    assert f"{camera.BULB_PATH}=0" in combined
    assert "--wait-event-and-download=FILEADDED" in combined
    assert combined[:2] == ("--filename", str(tmp_path / "light_0001.%C"))
    assert options[0]["timeout"] == 17
    assert len(calls) == 1
    assert camera.stop_bulb_attempts == 0


@pytest.mark.asyncio
async def test_physical_bulb_rejects_subsecond_exposure_before_camera_command(monkeypatch, tmp_path):
    camera = GPhotoCamera()

    async def unexpected_run(*args, **kwargs):
        pytest.fail("Subsecond Bulb request reached gphoto2")

    monkeypatch.setattr(camera, "_run", unexpected_run)
    with pytest.raises(CameraError, match="at least 1 second"):
        await camera.capture_bulb(0.5, tmp_path / "light_0001.%C", asyncio.Event())


@pytest.mark.asyncio
async def test_stop_during_download_keeps_current_camera_command_alive(monkeypatch, tmp_path):
    camera = GPhotoCamera()
    stop = asyncio.Event()
    release_download = asyncio.Event()

    async def fake_run(*args, **kwargs):
        if "--wait-event-and-download=FILEADDED" in args:
            await release_download.wait()
            (tmp_path / "light_0001.nef").write_bytes(b"nef")
        return CommandResult(0, "", "")

    async def downloading():
        stop.set()
        release_download.set()

    monkeypatch.setattr(camera, "_run", fake_run)
    result = await camera.capture_bulb(
        1, tmp_path / "light_0001.%C", stop, downloading_callback=downloading
    )
    assert result.cancelled
    assert result.path == tmp_path / "light_0001.nef"


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
    assert any("--wait-event=10s" in call for call in calls)
    assert all("--wait-event-and-download=FILEADDED" not in call for call in calls)


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
    assert camera.storage_choice(StorageDestination.COMPUTER) == "Memory card"
    assert camera.storage_choice(StorageDestination.CAMERA) == "Memory card"


@pytest.mark.asyncio
async def test_mac_capture_uses_previous_card_staging_target(monkeypatch):
    camera = GPhotoCamera()
    camera.config = CameraConfig(
        capture_target=ConfigProperty(
            path="/main/settings/capturetarget",
            available=True,
            current="Internal RAM",
            choices=["Internal RAM", "Memory card"],
        )
    )
    calls = []

    async def fake_set(path, value):
        calls.append((path, value))

    monkeypatch.setattr(camera, "set_config", fake_set)
    await camera.configure_storage(StorageDestination.COMPUTER)
    assert calls == [("/main/settings/capturetarget", "Memory card")]


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
async def test_nikon_focus_is_applied_inside_restarted_liveview_process(monkeypatch):
    camera = GPhotoCamera()
    camera.config = CameraConfig(
        viewfinder=ConfigProperty(path="/main/actions/viewfinder", available=True, current="0"),
        application_mode=ConfigProperty(
            path="/main/capturesettings/applicationmode",
            available=True,
            current="Application Mode 0",
            choices=["Application Mode 0", "Application Mode 1"],
        ),
        manual_focus=ConfigProperty(
            path="/main/actions/manualfocusdrive",
            available=True,
            minimum=-32767,
            maximum=32767,
        ),
        focus_mode=ConfigProperty(
            path="/main/capturesettings/focusmode2",
            available=True,
            current="AF-A",
            choices=["AF-A", "MF (selection)"],
        ),
        liveview_focus=ConfigProperty(
            path="/main/capturesettings/liveviewaffocus",
            available=True,
            current="Full-time-servo AF",
            choices=["Single-servo AF", "Full-time-servo AF"],
        ),
    )
    process = PersistentFakeProcess()
    spawn_calls = []

    async def fake_set(path, value):
        return None

    async def fake_exec(*args, **kwargs):
        spawn_calls.append(args)
        return process

    monkeypatch.setattr(camera, "set_config", fake_set)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await camera.start_liveview(focus_amount=-100)
    command = spawn_calls[0]
    assert "/main/actions/viewfinder=1" in command
    assert "/main/capturesettings/focusmode2=MF (selection)" in command
    assert "/main/capturesettings/liveviewaffocus=Single-servo AF" in command
    assert "/main/actions/manualfocusdrive=-100" in command
    assert command[-2:] == ("--stdout", "--capture-movie")
    await camera.stop_liveview(send_off=False)


@pytest.mark.asyncio
async def test_simulated_focus_requires_liveview_and_moves_relative_position():
    from app.camera import SimulatedCamera

    camera = SimulatedCamera(time_scale=0)
    with pytest.raises(CameraError, match="Start Live View"):
        await camera.adjust_focus(FocusDirection.NEAR, 1)
    await camera.start_liveview()
    amount = await camera.adjust_focus(FocusDirection.FAR, 3)
    assert amount == 2000
    assert camera._sim_focus_position == 2000


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


@pytest.mark.asyncio
async def test_card_capture_recovers_missing_event_from_new_card_file(monkeypatch):
    camera = GPhotoCamera()
    listings = iter([["/DCIM/old.NEF"], ["/DCIM/old.NEF", "/DCIM/new.NEF"]])

    async def files():
        return next(listings)

    async def run(*args, **kwargs):
        return CommandResult(0, "", "")

    monkeypatch.setattr(camera, "list_camera_files", files)
    monkeypatch.setattr(camera, "_run", run)
    result = await camera.capture_bulb(1, None, asyncio.Event(), download=False)
    assert result.camera_filename == "new.NEF"
    assert result.path is None


@pytest.mark.asyncio
async def test_card_confirmation_does_not_count_existing_files(monkeypatch):
    camera = GPhotoCamera()

    async def files():
        return ["/DCIM/old.NEF"]

    monkeypatch.setattr(camera, "list_camera_files", files)
    assert await camera._confirm_card_capture(
        {"/DCIM/old.NEF"}, asyncio.Event(), timeout=0
    ) is None


@pytest.mark.asyncio
async def test_card_confirmation_recovers_delayed_event(monkeypatch):
    camera = GPhotoCamera()

    async def files():
        return []

    async def run(*args, **kwargs):
        return CommandResult(0, "FILEADDED /DCIM/new.NEF", "")

    monkeypatch.setattr(camera, "list_camera_files", files)
    monkeypatch.setattr(camera, "_run", run)
    assert await camera._confirm_card_capture(set(), asyncio.Event(), timeout=30) == "new.NEF"


@pytest.mark.asyncio
async def test_card_confirmation_respects_stop(monkeypatch):
    camera = GPhotoCamera()
    stop = asyncio.Event()
    stop.set()
    assert await camera._confirm_card_capture(set(), stop, timeout=30) is None
