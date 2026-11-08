"""CDN MIME regressions. Synthetic images, loopback HTTP only, no account or generation."""
import base64
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as module

FIXTURES = Path(__file__).with_name("fixtures")


def response(data, mime):
    result = io.BytesIO(data)
    result.headers = Message()
    result.headers["Content-Type"] = mime
    return result


class ReferenceMimeHeadersTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.cfg = SimpleNamespace(profile_dir=self.home.name,
                                   ref_image_private_fetch=False,
                                   ref_image_local_files=False)
        self.engine = module.MuseEngine(self.cfg)

    def remote(self, data, mime, suffix="jpg"):
        with patch.object(module, "_open_public_ref_image",
                          return_value=response(data, mime)) as fetch:
            result = self.engine._normalize_image(
                "https://cdn.example/reference." + suffix)
            fetch.assert_called_once()
            return base64.b64decode(result[0], validate=True), result[1]

    def test_raw_cdn_images_with_multipart_header_keep_exact_original_bytes(self):
        # The production failure was a ~13 KB WebP behind a .jpg URL with
        # Content-Type multipart/form-data, not a multipart envelope.
        for extension, mime in (("webp", "image/webp"), ("jpeg", "image/jpeg"),
                                ("png", "image/png")):
            data = (FIXTURES / ("reference-header." + extension)).read_bytes()
            if extension == "webp":
                self.assertGreater(len(data), 12_000)
                self.assertLess(len(data), 14_000)
            with self.subTest(extension=extension):
                self.assertEqual(self.remote(data, "Multipart/Form-Data; boundary=wrong"),
                                 (data, mime))

    def test_declared_image_mime_is_corrected_to_actual_image_signature(self):
        data = (FIXTURES / "reference-header.webp").read_bytes()
        self.assertEqual(self.remote(data, "image/jpeg"), (data, "image/webp"))
        self.assertEqual(self.remote(data, "application/octet-stream"),
                         (data, "image/webp"))

    def test_error_pages_and_documents_cannot_masquerade_as_images(self):
        payloads = (b"<!doctype html><html>login required</html>",
                    b"\xef\xbb\xbf \n{\"error\":\"not found\"}",
                    b"[\"not an image\"]", b"%PDF-1.7\nfixture")
        for mime in ("multipart/form-data", "image/png", "application/octet-stream"):
            for data in payloads:
                with self.subTest(mime=mime, prefix=data[:4]):
                    with self.assertRaisesRegex(module.MuseGenerationError,
                                                "参考文件类型不受支持"):
                        self.remote(data, mime)

    def test_actual_multipart_envelope_is_not_misread_as_raw_image(self):
        png = (FIXTURES / "reference-header.png").read_bytes()
        data = (b"--fixture\r\nContent-Type: image/png\r\n\r\n" + png
                + b"\r\n--fixture--\r\n")
        with self.assertRaisesRegex(module.MuseGenerationError, "参考文件类型不受支持"):
            self.remote(data, "multipart/form-data; boundary=fixture")

    def test_extension_alone_cannot_promote_unknown_bytes_to_an_image(self):
        with self.assertRaisesRegex(module.MuseGenerationError, "参考文件类型不受支持"):
            self.remote(b"not an image", "application/octet-stream", "jpg")
        with self.assertRaisesRegex(module.MuseGenerationError, "参考文件类型不受支持"):
            self.remote(b"%PDF-1.7\nfixture", "multipart/form-data", "pdf")
        # Explicit document references remain supported by the shared normalizer.
        data = b"%PDF-1.7\nfixture"
        self.assertEqual(self.remote(data, "application/pdf", "pdf"),
                         (data, "application/pdf"))

    def test_real_http_404_is_rejected_with_status_without_url_or_body(self):
        class Missing(BaseHTTPRequestHandler):
            calls = 0

            def do_GET(self):
                type(self).calls += 1
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"fixture private response body")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Missing)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.cfg.ref_image_private_fetch = True  # Only this explicit loopback fixture.
        try:
            with self.assertRaises(module.MuseGenerationError) as caught:
                self.engine._normalize_image(
                    f"http://127.0.0.1:{server.server_port}/missing.png?token=fixture-only")
            self.assertEqual(str(caught.exception), "下载远程参考图失败: HTTP 404")
            self.assertNotIn("token", str(caught.exception))
            self.assertNotIn("response body", str(caught.exception))
            self.assertEqual(Missing.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
