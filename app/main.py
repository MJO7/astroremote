from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .camera import CameraError, GPhotoCamera, SimulatedCamera
from .models import (
    CameraConfig,
    CameraDisplayMode,
    CameraDisplayRequest,
    CameraInfo,
    CameraMediaItem,
    FocusRequest,
    FocusResult,
    LiveViewStatus,
    RuntimeInfo,
    SequenceRequest,
    SequenceStatus,
)
from .platform_support import CameraOwnershipHelper
from .sequence import SequenceConflictError, SequenceController, SequenceValidationError


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def likely_lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        address = sock.getsockname()[0]
        return address if not address.startswith("127.") else None
    except OSError:
        return None
    finally:
        sock.close()


def build_services(*, simulate: bool | None = None, output_root: Path | None = None, time_scale: float = 1.0):
    simulation = env_flag("ASTROREMOTE_SIMULATE") if simulate is None else simulate
    root = output_root or Path(os.getenv("ASTROREMOTE_OUTPUT_ROOT", "~/Pictures/AstroRemote"))
    camera = SimulatedCamera(time_scale=time_scale) if simulation else GPhotoCamera()
    controller = SequenceController(camera, root)
    camera.debug_callback = controller.debug
    helper = CameraOwnershipHelper()
    return camera, controller, helper


def create_app(
    *,
    simulate: bool | None = None,
    output_root: Path | None = None,
    time_scale: float = 1.0,
) -> FastAPI:
    camera, controller, helper = build_services(
        simulate=simulate, output_root=output_root, time_scale=time_scale
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        port = int(os.getenv("ASTROREMOTE_PORT", "8000"))
        print("\nAstroRemote running.\n")
        print(f"Computer:\nhttp://localhost:{port}\n")
        lan_ip = likely_lan_ip()
        if lan_ip:
            print(f"Phone/tablet:\nhttp://{lan_ip}:{port}\n")
        try:
            await controller.refresh_camera()
        except Exception as exc:
            controller.log(f"Initial camera check failed: {exc}", "error")
        yield
        await controller.shutdown()

    app = FastAPI(title="AstroRemote", version="1.0.0", lifespan=lifespan)
    app.state.camera = camera
    app.state.controller = controller
    app.state.ownership_helper = helper
    app.state.liveview_owner = None
    liveview_api_lock = asyncio.Lock()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/liveview", include_in_schema=False)
    async def liveview_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "liveview.html", headers={"Cache-Control": "no-store"})

    @app.get("/playback", include_in_schema=False)
    async def playback_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "playback.html", headers={"Cache-Control": "no-store"})

    @app.get("/manifest.json", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")

    @app.get("/api/runtime", response_model=RuntimeInfo)
    async def runtime_info() -> RuntimeInfo:
        return RuntimeInfo(
            output_root=str(controller.output_root),
            simulation=camera.info.simulation,
            ptp_helper_configured=helper.configured,
        )

    @app.get("/api/camera", response_model=CameraInfo)
    async def get_camera() -> CameraInfo:
        return controller.camera_info

    @app.get("/api/camera/config", response_model=CameraConfig)
    async def get_camera_config() -> CameraConfig:
        return controller.camera_config

    @app.post("/api/camera/refresh", response_model=CameraInfo)
    async def refresh_camera() -> CameraInfo:
        try:
            info, _ = await controller.refresh_camera()
            return info
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/camera/release", response_model=CameraInfo)
    async def release_camera() -> CameraInfo:
        try:
            detail, info, _ = await controller.release_camera_services(helper.release)
            controller.log(detail)
            return info
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/camera/display", response_model=CameraConfig)
    async def set_camera_display(configuration: CameraDisplayRequest) -> CameraConfig:
        try:
            return await controller.set_camera_display(configuration.mode)
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SequenceValidationError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/liveview/status", response_model=LiveViewStatus)
    async def liveview_status() -> LiveViewStatus:
        return controller.liveview_status()

    @app.post("/api/liveview/start", response_model=LiveViewStatus)
    async def start_liveview(client_id: str | None = None) -> LiveViewStatus:
        try:
            async with liveview_api_lock:
                await controller.set_camera_display(CameraDisplayMode.LIVEVIEW)
                app.state.liveview_owner = client_id
                return controller.liveview_status()
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SequenceValidationError, CameraError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/liveview/stop", response_model=LiveViewStatus)
    async def stop_liveview(client_id: str | None = None) -> LiveViewStatus:
        try:
            async with liveview_api_lock:
                # Ignore a late page-unload beacon after a replacement viewer has
                # already taken ownership (notably during a browser reload).
                if client_id and app.state.liveview_owner not in {None, client_id}:
                    return controller.liveview_status()
                await controller.set_camera_display(CameraDisplayMode.PLAYBACK)
                app.state.liveview_owner = None
                return controller.liveview_status()
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SequenceValidationError, CameraError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/liveview/stream")
    async def liveview_stream(request: Request) -> StreamingResponse:
        if not camera.liveview_running:
            raise HTTPException(status_code=409, detail=camera.liveview_error or "Live View is not running")

        async def frames():
            frame_id = 0
            try:
                while not await request.is_disconnected():
                    frame_id, frame, content_type = await controller.next_liveview_frame(frame_id)
                    yield (
                        b"--frame\r\n"
                        + f"Content-Type: {content_type}\r\nContent-Length: {len(frame)}\r\n\r\n".encode()
                        + frame
                        + b"\r\n"
                    )
            except (CameraError, asyncio.TimeoutError):
                return

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/liveview/focus", response_model=FocusResult)
    async def adjust_liveview_focus(configuration: FocusRequest) -> FocusResult:
        try:
            async with liveview_api_lock:
                return await controller.adjust_focus(configuration.direction, configuration.step)
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SequenceValidationError, CameraError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/gallery", response_model=list[CameraMediaItem])
    async def camera_gallery() -> list[CameraMediaItem]:
        try:
            return await controller.list_camera_media()
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SequenceValidationError, CameraError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/gallery/thumbnail")
    async def camera_gallery_thumbnail(path: str) -> Response:
        try:
            content, content_type = await controller.get_camera_thumbnail(path)
            return Response(
                content=content,
                media_type=content_type,
                headers={"Cache-Control": "private, max-age=300"},
            )
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SequenceValidationError, CameraError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/status", response_model=SequenceStatus)
    async def get_status() -> SequenceStatus:
        return controller.status

    @app.post("/api/sequence/start", response_model=SequenceStatus, status_code=202)
    async def start_sequence(configuration: SequenceRequest) -> SequenceStatus:
        try:
            return await controller.start(configuration)
        except SequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SequenceValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/sequence/stop", response_model=SequenceStatus, status_code=202)
    async def stop_sequence() -> SequenceStatus:
        return await controller.stop()

    @app.get("/api/logs")
    async def get_logs(debug: bool = False) -> dict[str, list[dict]]:
        entries = controller.debug_logs if debug else controller.logs
        return {"logs": [entry.model_dump(mode="json") for entry in entries]}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = controller.events.subscribe()

        async def stream():
            try:
                initial = {"type": "status", "data": controller.status.model_dump(mode="json")}
                yield f"data: {json.dumps(initial)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                controller.events.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
