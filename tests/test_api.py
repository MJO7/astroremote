import time

from fastapi.testclient import TestClient

from app.main import create_app


def test_status_can_be_retrieved_after_browser_reconnect(tmp_path):
    app = create_app(simulate=True, output_root=tmp_path, time_scale=0.1)
    with TestClient(app) as first_client:
        payload = {
            "exposure": 2,
            "interval": 0,
            "shots": 2,
            "iso": "800",
            "aperture": "f/2.8",
            "output_subdirectory": "night-one",
        }
        response = first_client.post("/api/sequence/start", json=payload)
        assert response.status_code == 202
        time.sleep(0.04)

        # Status retrieval is independent of the event stream/browser session.
        # A reconnecting Safari instance performs this same stateless request.
        status = first_client.get("/api/status").json()
        assert status["sequence_total"] == 2
        assert status["active"] or status["sequence_completed"] == 2


def test_api_rejects_second_sequence(tmp_path):
    app = create_app(simulate=True, output_root=tmp_path)
    payload = {"exposure": 10, "interval": 0, "shots": 1, "iso": "800", "aperture": "f/2.8"}
    with TestClient(app) as client:
        assert client.post("/api/sequence/start", json=payload).status_code == 202
        assert client.post("/api/sequence/start", json=payload).status_code == 409
        assert client.post("/api/sequence/stop").status_code == 202


def test_camera_display_api(tmp_path):
    app = create_app(simulate=True, output_root=tmp_path)
    with TestClient(app) as client:
        liveview = client.post("/api/camera/display", json={"mode": "liveview"})
        assert liveview.status_code == 200
        assert liveview.json()["viewfinder"]["current"] == "1"
        playback = client.post("/api/camera/display", json={"mode": "playback"})
        assert playback.status_code == 200
        assert playback.json()["application_mode"]["current"] == "Application Mode 1"


def test_liveview_page_and_lifecycle_api(tmp_path):
    app = create_app(simulate=True, output_root=tmp_path)
    with TestClient(app) as client:
        page = client.get("/liveview")
        assert page.status_code == 200
        assert "CAMERA LIVE VIEW" in page.text

        started = client.post("/api/liveview/start")
        assert started.status_code == 200
        assert started.json()["active"] is True

        status = client.get("/api/liveview/status")
        assert status.json()["active"] is True

        stopped = client.post("/api/liveview/stop")
        assert stopped.status_code == 200
        assert stopped.json()["active"] is False


def test_camera_gallery_page_list_and_thumbnail(tmp_path):
    app = create_app(simulate=True, output_root=tmp_path)
    with TestClient(app) as client:
        page = client.get("/playback")
        assert page.status_code == 200
        assert "CAMERA GALLERY" in page.text

        response = client.get("/api/gallery")
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 18
        assert items[0]["name"] == "DSC_9018.NEF"
        assert items[0]["previewable"] is True

        thumbnail = client.get("/api/gallery/thumbnail", params={"path": items[0]["path"]})
        assert thumbnail.status_code == 200
        assert thumbnail.headers["content-type"].startswith("image/svg+xml")
        assert b"DSC_9018.NEF" in thumbnail.content

        missing = client.get("/api/gallery/thumbnail", params={"path": "/not-indexed.NEF"})
        assert missing.status_code == 404

