import json

from lite_recorder import recordings


def test_list_sessions_empty(tmp_path):
    assert recordings.list_sessions(tmp_path / "nonexistent") == []


def test_list_sessions_reads_manifest(tmp_path):
    root = tmp_path / "recordings"
    session_dir = root / "19-08-2026" / "22-32-46"
    session_dir.mkdir(parents=True)
    (session_dir / "cam1.mp4").write_bytes(b"x" * 100)
    manifest = {
        "started_at": "2026-08-19T22:32:46",
        "ended_at": "2026-08-19T22:33:00",
        "cameras": {"sim0": {"label": "Front Door", "file": "cam1.mp4", "status": "complete"}},
    }
    (session_dir / "session.json").write_text(json.dumps(manifest))

    sessions = recordings.list_sessions(root)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.date == "19-08-2026"
    assert s.time == "22-32-46"
    assert len(s.files) == 1
    assert s.files[0].label == "Front Door"
    assert s.files[0].size_bytes == 100


def test_list_sessions_skips_empty_dirs(tmp_path):
    root = tmp_path / "recordings"
    (root / "19-08-2026" / "10-00-00").mkdir(parents=True)
    assert recordings.list_sessions(root) == []


def test_list_sessions_newest_first(tmp_path):
    root = tmp_path / "recordings"
    for date, t in [("18-08-2026", "10-00-00"), ("19-08-2026", "09-00-00")]:
        d = root / date / t
        d.mkdir(parents=True)
        (d / "cam1.mp4").write_bytes(b"x")
    sessions = recordings.list_sessions(root)
    assert sessions[0].date == "19-08-2026"
    assert sessions[1].date == "18-08-2026"


def test_delete_recording(tmp_path):
    root = tmp_path / "recordings"
    d = root / "19-08-2026" / "22-32-46"
    d.mkdir(parents=True)
    f = d / "cam1.mp4"
    f.write_bytes(b"x")
    recordings.delete_recording(root, "19-08-2026/22-32-46/cam1.mp4")
    assert not f.exists()


def test_delete_recording_blocks_path_traversal(tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    try:
        recordings.delete_recording(root, "../secret.txt")
        assert False, "should have raised"
    except ValueError:
        pass
    assert outside.exists()


def test_delete_recording_missing_file_raises(tmp_path):
    root = tmp_path / "recordings"
    root.mkdir()
    try:
        recordings.delete_recording(root, "19-08-2026/22-32-46/missing.mp4")
        assert False, "should have raised"
    except FileNotFoundError:
        pass
