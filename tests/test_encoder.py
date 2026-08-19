from lite_recorder import encoder


def test_build_input_args_simulate():
    args = encoder.build_input_args("/dev/video0", "MJPG", 640, 480, 30, simulate=True)
    assert args == ["-f", "lavfi", "-i", "testsrc=size=640x480:rate=30"]


def test_build_input_args_real_mjpeg():
    args = encoder.build_input_args("/dev/video0", "MJPG", 1280, 720, 30, simulate=False)
    assert args == [
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", "1280x720", "-framerate", "30", "-i", "/dev/video0",
    ]


def test_build_input_args_real_yuyv():
    args = encoder.build_input_args("/dev/video2", "YUYV", 640, 480, 15, simulate=False)
    assert "-input_format" in args
    assert args[args.index("-input_format") + 1] == "yuyv422"


def test_build_ffmpeg_command_preview_only():
    cmd = encoder.build_ffmpeg_command(
        ffmpeg_bin="ffmpeg", device="/dev/video0", pixel_format="MJPG",
        width=640, height=480, fps=30, preview_width=320, preview_fps=10,
        simulate=True,
    )
    assert cmd[0] == "ffmpeg"
    assert "pipe:1" in cmd
    assert "-c:v" not in cmd  # no recording leg


def test_build_ffmpeg_command_with_recording():
    enc = encoder.EncoderInfo(name="libx264", kind="software", degraded=True)
    cmd = encoder.build_ffmpeg_command(
        ffmpeg_bin="ffmpeg", device="/dev/video0", pixel_format="MJPG",
        width=640, height=480, fps=30, preview_width=320, preview_fps=10,
        simulate=True, encoder=enc, output_path="/tmp/out.mp4", bitrate_kbps=2000,
    )
    assert "/tmp/out.mp4" in cmd
    assert "pipe:1" in cmd
    assert cmd.count("-map") == 2  # one leg for file, one for preview
    assert "libx264" in cmd
    assert "2000k" in cmd


def test_select_encoder_falls_back_to_software(monkeypatch):
    def fake_list(ffmpeg_bin):
        return {"libx264", "h264_rkmpp"}

    def fake_validate(ffmpeg_bin, name):
        if name == "h264_rkmpp":
            return False, "no /dev/mpp_service"
        return True, ""

    monkeypatch.setattr(encoder, "_list_available_encoders", fake_list)
    monkeypatch.setattr(encoder, "_validate_encoder", fake_validate)

    info = encoder.select_encoder("ffmpeg")
    assert info.name == "libx264"
    assert info.kind == "software"
    assert info.degraded is True
    assert "h264_rkmpp" in info.reason


def test_select_encoder_prefers_hardware(monkeypatch):
    monkeypatch.setattr(encoder, "_list_available_encoders", lambda b: {"h264_rkmpp", "libx264"})
    monkeypatch.setattr(encoder, "_validate_encoder", lambda b, n: (True, ""))

    info = encoder.select_encoder("ffmpeg")
    assert info.name == "h264_rkmpp"
    assert info.kind == "hardware"
    assert info.degraded is False


def test_select_encoder_force(monkeypatch):
    monkeypatch.setattr(encoder, "_list_available_encoders", lambda b: set())
    monkeypatch.setattr(encoder, "_validate_encoder", lambda b, n: (True, ""))

    info = encoder.select_encoder("ffmpeg", force="libx264")
    assert info.name == "libx264"


def test_select_encoder_nothing_works(monkeypatch):
    monkeypatch.setattr(encoder, "_list_available_encoders", lambda b: {"libx264"})
    monkeypatch.setattr(encoder, "_validate_encoder", lambda b, n: (False, "boom"))

    info = encoder.select_encoder("ffmpeg")
    assert info.degraded is True
    assert "No working encoder" in info.reason
