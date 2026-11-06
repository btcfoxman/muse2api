"""Measure downloaded original videos without treating requested specs as facts."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine


class VideoMetadataTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="muse-video-metadata-")
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        self.source = self.root / "original ; '$(never-run).mp4"
        self.source.write_bytes(b"original media bytes")

    def probe(self, body, *, code=0):
        result = SimpleNamespace(returncode=code, stdout=json.dumps(body).encode())
        with patch.object(engine.shutil, "which", return_value="ffprobe"), \
                patch.object(engine.subprocess, "run", return_value=result) as run:
            measured = engine.probe_generated_video(str(self.source))
        return measured, run

    def test_measurement_comes_from_video_stream_not_a_request(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        measured, run = self.probe({"streams": [{"width": 1280, "height": 720, "duration": "6.04"}],
                                    "format": {"duration": "6.08"}})
        self.assertEqual(measured, {"width": 1280, "height": 720, "duration": 6.04,
                                    "measured_by": "ffprobe"})
        args, options = run.call_args
        self.assertEqual(args[0][-1], str(self.source.resolve()))
        self.assertIs(options["shell"], False)
        self.assertEqual(options["timeout"], 20)
        self.assertIn("file,pipe", args[0])
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_format_duration_is_used_when_stream_duration_is_missing(self):
        measured, _ = self.probe({"streams": [{"width": 2160, "height": 3840}],
                                  "format": {"duration": "29.97"}})
        self.assertEqual(measured["duration"], 29.97)
        self.assertEqual((measured["width"], measured["height"]), (2160, 3840))

    def test_missing_tool_or_source_does_not_spawn_or_fail(self):
        with patch.object(engine.shutil, "which", return_value=None), \
                patch.object(engine.subprocess, "run") as run:
            self.assertIsNone(engine.probe_generated_video(str(self.source)))
            run.assert_not_called()
        with patch.object(engine.shutil, "which", return_value="ffprobe"), \
                patch.object(engine.subprocess, "run") as run:
            self.assertIsNone(engine.probe_generated_video(str(self.root / "missing.mp4")))
            run.assert_not_called()

    def test_malformed_unknown_or_nonfinite_metadata_is_not_reported_as_measured(self):
        for body in [None, [], {}, {"streams": []}, {"streams": [None]},
                     {"streams": [{"width": True, "height": 720, "duration": "6"}]},
                     {"streams": [{"width": 1280, "height": 0, "duration": "6"}]},
                     {"streams": [{"width": 1280, "height": 720, "duration": "nan"}]},
                     {"streams": [{"width": 1280, "height": 720, "duration": "inf"}]},
                     {"streams": [{"width": 1280, "height": 720, "duration": -1}]},
                     {"streams": [{"width": 1280, "height": 720, "duration": True}]}]:
            with self.subTest(body=body):
                self.assertIsNone(self.probe(body)[0])

    def test_probe_transport_and_process_failures_leave_original_unchanged(self):
        before = self.source.read_bytes()
        for exception in (OSError("unavailable"), subprocess.TimeoutExpired("ffprobe", 20)):
            with self.subTest(exception=type(exception).__name__), \
                    patch.object(engine.shutil, "which", return_value="ffprobe"), \
                    patch.object(engine.subprocess, "run", side_effect=exception):
                self.assertIsNone(engine.probe_generated_video(str(self.source)))
        self.assertIsNone(self.probe({"streams": []}, code=1)[0])
        self.assertEqual(self.source.read_bytes(), before)

    def generation(self, *, fallback=False, measured=None):
        e = engine.MuseEngine(SimpleNamespace(profile_dir=str(self.root), media_dir=str(self.root)))
        e.attachments = lambda: []
        e._agent_count = lambda: 0
        e._debug_dump = lambda _: None
        sent = []
        e._send = lambda prompt: sent.append(prompt) or "clicked"
        e._wait_attachment = lambda *args, **kwargs: {
            "src": "blob:generated", "vSrc": "blob:generated", "tid": "generated", "w": 16, "h": 9}
        if fallback:
            e.extract_bytes = lambda *args, **kwargs: (None, None, None)
            e._download_fallback = lambda _: str(self.source)
        else:
            e.extract_bytes = lambda *args, **kwargs: (b"original media bytes", "video/mp4", "blob:generated")
        with patch.object(engine, "probe_generated_video", return_value=measured) as probe:
            result = e._generate_attached("generate once", "video", time.monotonic() + 30,
                                          None, None, set())
        self.assertEqual(sent, ["generate once"])
        self.assertEqual(Path(result["path"]).read_bytes(), b"original media bytes")
        probe.assert_called_once_with(result["path"])
        return result

    def test_blob_delivery_returns_measured_original_metadata(self):
        actual = {"width": 1280, "height": 720, "duration": 6.0, "measured_by": "ffprobe"}
        result = self.generation(measured=actual)
        self.assertEqual(result["media_metadata"], actual)
        self.assertEqual(result["media_metadata_status"], "measured")

    def test_download_delivery_returns_measured_original_metadata(self):
        actual = {"width": 1920, "height": 1080, "duration": 10.0, "measured_by": "ffprobe"}
        result = self.generation(fallback=True, measured=actual)
        self.assertEqual(result["media_metadata"], actual)
        self.assertEqual(result["media_metadata_status"], "measured")

    def test_probe_unavailable_does_not_repeat_or_fail_generation(self):
        result = self.generation(measured=None)
        self.assertIsNone(result["media_metadata"])
        self.assertEqual(result["media_metadata_status"], "unavailable")
        self.assertEqual(result["kind"], "video")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
    def test_real_ffprobe_reads_fixture_original_without_changing_bytes(self):
        # Local synthetic fixture; no Muse/browser/account/network/generation API.
        result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi",
                                 "-i", "color=c=blue:s=320x240:r=10", "-t", "1.2", "-an",
                                 "-c:v", "mpeg4", "-y", str(self.source)],
                                capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        measured = engine.probe_generated_video(str(self.source))
        self.assertEqual((measured["width"], measured["height"]), (320, 240))
        self.assertAlmostEqual(measured["duration"], 1.2, places=1)
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
