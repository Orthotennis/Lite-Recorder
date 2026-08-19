import pytest
from fastapi.testclient import TestClient

from lite_recorder.config import Settings
from lite_recorder.app import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        recordings_root=tmp_path / "recordings",
        state_dir=tmp_path / "state",
        simulate=True,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_system_endpoint(client):
    res = client.get("/api/system")
    assert res.status_code == 200
    data = res.json()
    assert data["simulate"] is True
    assert data["camera_count"] == 4
    assert "encoder" in data


def test_list_cameras(client):
    res = client.get("/api/cameras")
    assert res.status_code == 200
    cams = res.json()
    assert len(cams) == 4
    assert all(c["state"] == "preview" for c in cams)


def test_patch_camera(client):
    cam_id = client.get("/api/cameras").json()[0]["id"]
    res = client.patch(f"/api/cameras/{cam_id}", json={"label": "front-door", "fps": 24})
    assert res.status_code == 200
    assert res.json()["label"] == "front-door"
    assert res.json()["fps"] == 24


def test_patch_unknown_camera_404(client):
    res = client.patch("/api/cameras/nope", json={"label": "x"})
    assert res.status_code == 404


def test_rescan(client):
    res = client.post("/api/cameras/rescan")
    assert res.status_code == 200
    assert len(res.json()) == 4


def test_recording_status_when_idle(client):
    res = client.get("/api/recording/status")
    assert res.json() == {"recording": False}


def test_stop_without_start_returns_409(client):
    res = client.post("/api/recording/stop")
    assert res.status_code == 409


def test_recording_lifecycle(client):
    start = client.post("/api/recording/start")
    assert start.status_code == 200
    assert all(r["ok"] for r in start.json()["results"])

    status = client.get("/api/recording/status")
    assert status.json()["recording"] is True

    double_start = client.post("/api/recording/start")
    assert double_start.status_code == 409

    import time
    time.sleep(1.2)

    stop = client.post("/api/recording/stop")
    assert stop.status_code == 200

    gallery = client.get("/api/recordings").json()
    assert len(gallery) == 1
    assert len(gallery[0]["files"]) == 4
    for f in gallery[0]["files"]:
        assert f["status"] == "complete"
        assert f["size_bytes"] > 0

    # playable via /media with range support
    url = gallery[0]["files"][0]["url"]
    media_res = client.get(url, headers={"Range": "bytes=0-99"})
    assert media_res.status_code == 206


def test_snapshot_endpoint(client):
    cam_id = client.get("/api/cameras").json()[0]["id"]
    import time
    for _ in range(20):
        res = client.get(f"/api/cameras/{cam_id}/snapshot.jpg")
        if res.status_code == 200:
            break
        time.sleep(0.2)
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_snapshot_unknown_camera_404(client):
    res = client.get("/api/cameras/nope/snapshot.jpg")
    assert res.status_code == 404


def test_delete_recording_path_traversal_blocked(client):
    res = client.delete("/api/recordings/../../etc/passwd")
    assert res.status_code == 404


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Lite-Recorder" in res.text
