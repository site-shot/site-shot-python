"""No live API calls: the runnable report is tested through fake clients."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from PIL import Image

from examples import csv_report as report
from site_shot import APIError, AuthError, SiteShotTimeoutError


def png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (12, 8), "white").save(stream, format="PNG")
    return stream.getvalue()


class FakeClient:
    def __init__(self, response: Any = None) -> None:
        self.response = png_bytes() if response is None else response
        self.calls: list[Any] = []

    def capture(self, url: str, /, **params: Any) -> bytes:
        self.calls.append((url, params))
        if isinstance(self.response, Exception):
            raise self.response
        return bytes(self.response)


class CSVReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def csv(self, text: str) -> Path:
        path = self.root / "input.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_public_fixture_has_five_real_owned_pages(self) -> None:
        rows = report.read_rows(Path(__file__).parents[1] / "examples/report_urls.csv")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r.url.startswith("https://www.site-shot.com/") for r in rows))

    def test_bad_csv_rejected(self) -> None:
        for text in (
            "url,id\nhttps://example.com,a",
            "id,url\n",
            "id,url\na,https://example.com\nA,https://example.com/b",
            "id,url\n../a,https://example.com",
            "id,url\nCON,https://example.com",
            "id,url\na,https://example.com,extra",
            'id,url\na,"broken',
        ):
            with self.subTest(text=text), self.assertRaises(report.InputError):
                report.read_rows(self.csv(text))

    def test_bom_and_stable_ids(self) -> None:
        rows = report.read_rows(
            self.csv("\ufeffid,url\na-1,https://example.com/\nb_2,https://example.org/\n")
        )
        self.assertEqual([r.id for r in rows], ["a-1", "b_2"])

    def test_untrusted_url_inputs(self) -> None:
        for url in (
            "javascript:alert(1)",
            "https://user:secret@example.com/",
            "https://example.com/?token=secret",
            "https://example.com/#secret",
            "http://127.0.0.1/",
            "http://127.1/",
            "http://0177.0.0.1/",
            "http://0x7f.0.0.1/",
            "http://127.0.0.1./",
            "http://192.168.1/",
            "http://2130706433/",
            "http://１２７.０.０.１/",
            "http://127。0。0。1/",
            "http://０１７７.０.０.１/",
            "http://０ｘ７ｆ.０.０.１/",
            "http://127.0.0.1。/",
            "http://[::1]/",
            "http://localhost/",
            "https://test.local/",
            "https://example.com:8080/",
            "https://example.com/\nx",
            "https://example.com\\@other.org/",
            "https://bad_host.com/",
        ):
            with self.subTest(url=url), self.assertRaises(report.InputError):
                report.validate_url(url)

    def test_public_idns_and_global_ip_literals_remain_allowed(self) -> None:
        for url in (
            "https://bücher.de/",
            "https://münchen.de/",
            "https://8.8.8.8/",
            "https://[2001:4860:4860::8888]/",
        ):
            with self.subTest(url=url):
                report.validate_url(url)

    def test_binary_validation_requires_decoded_png(self) -> None:
        self.assertEqual(report.validate_png(png_bytes()), (12, 8))
        jpg = BytesIO()
        Image.new("RGB", (12, 8)).save(jpg, format="JPEG")
        for data in (
            b'{"error":"secret"}',
            b"<html>Error</html>",
            jpg.getvalue(),
            b"\x89PNG\r\n\x1a\n{}",
            png_bytes()[:40],
        ):
            with self.subTest(data=data[:8]), self.assertRaises(report.InvalidImage):
                report.validate_png(data)
        with patch.object(report, "MAX_IMAGE_PIXELS", 2), self.assertRaises(report.InvalidImage):
            report.validate_png(png_bytes())

    def test_unique_run_folders_and_settings(self) -> None:
        client = FakeClient()
        rows = [report.Row("a", "https://example.com/")]
        first = report.run_batch(rows, self.root, lambda: client)
        second = report.run_batch(rows, self.root, lambda: client)
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(first.exit_code, 0)
        self.assertEqual((first.directory / "images/a.png").read_bytes(), png_bytes())
        manifest = json.loads((first.directory / "manifest.json").read_text())
        self.assertEqual(manifest["client_retries"], 0)
        self.assertEqual(manifest["workers"], 1)
        self.assertEqual(manifest["capture_settings"], report.CAPTURE_SETTINGS)
        self.assertEqual(manifest["rows"][0]["visual_review"], "required")
        self.assertTrue(all(params == report.CAPTURE_SETTINGS for _, params in client.calls))

    def test_failure_is_visible_without_secret_or_retry(self) -> None:
        secret = "secret-key-do-not-log"
        for error in (
            AuthError(secret, http_status=401, body=secret),
            SiteShotTimeoutError(secret),
            APIError(secret),
            ValueError(secret),
            b'{"error":"' + secret.encode() + b'"}',
        ):
            client = FakeClient(error)
            factory = Mock(return_value=client)
            run = report.run_batch([report.Row("a", "https://example.com/")], self.root, factory)
            self.assertEqual(run.exit_code, 1)
            self.assertEqual(len(client.calls), 1)
            manifest = (run.directory / "manifest.json").read_text()
            html = (run.directory / "index.html").read_text()
            self.assertNotIn(secret, manifest + html)
            self.assertIn("Incomplete capture batch", html)
            self.assertFalse((run.directory / "images/a.png").exists())

    def test_html_escapes_url_path_and_shows_failed_row(self) -> None:
        rows = [
            report.Row("a", 'https://example.com/<script>"&'),
            report.Row("b", "https://example.org/"),
        ]
        responses = iter([FakeClient(), FakeClient(b"not image")])
        run = report.run_batch(rows, self.root, lambda: next(responses))
        html = (run.directory / "index.html").read_text()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;&quot;&amp;", html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn("invalid_png", html)
        self.assertEqual((run.captured, run.failed, run.exit_code), (1, 1, 1))

    def test_workers_are_bounded_and_csv_order_preserved(self) -> None:
        lock = threading.Lock()
        active = 0
        peak = 0

        class ConcurrentClient:
            def capture(self, url: str, /, **params: Any) -> bytes:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.02)
                with lock:
                    active -= 1
                return png_bytes()

        rows = [report.Row(f"r{i}", f"https://example.com/{i}") for i in range(6)]
        run = report.run_batch(rows, self.root, ConcurrentClient, workers=2)
        self.assertEqual(peak, 2)
        data = json.loads((run.directory / "manifest.json").read_text())
        self.assertEqual([r["id"] for r in data["rows"]], [r.id for r in rows])
        for workers in (0, 9):
            with self.assertRaises(report.InputError):
                report.run_batch(rows, self.root, ConcurrentClient, workers=workers)

    def test_invalid_last_row_makes_no_calls(self) -> None:
        client = FakeClient()
        with self.assertRaises(report.InputError):
            report.run_batch(
                [
                    report.Row("a", "https://example.com/"),
                    report.Row("../b", "https://example.com/"),
                ],
                self.root,
                lambda: client,
            )
        self.assertEqual(client.calls, [])

    def test_interrupt_cancels_queued_work_during_submit_and_wait(self) -> None:
        for during_submit in (True, False):
            with self.subTest(during_submit=during_submit):
                self.assert_interrupt_cancels_pending(during_submit)

    def assert_interrupt_cancels_pending(self, during_submit: bool) -> None:
        started, release = threading.Event(), threading.Event()
        calls: list[str] = []

        class GatedClient:
            def capture(self, url: str, /, **params: Any) -> bytes:
                calls.append(url)
                started.set()
                if not release.wait(3):
                    raise RuntimeError("Test worker was not released")
                return png_bytes()

        class GatedExecutor(ThreadPoolExecutor):
            submissions = 0

            def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
                self.submissions += 1
                if during_submit and self.submissions == 6:
                    if not started.wait(3):
                        raise RuntimeError("Test capture did not start")
                    raise KeyboardInterrupt
                return super().submit(fn, *args, **kwargs)

            def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
                # Release in-flight work only after observing the real
                # executor's cancellation choice, without timing races.
                super().shutdown(wait=False, cancel_futures=cancel_futures)
                release.set()
                super().shutdown(wait=wait, cancel_futures=cancel_futures)

        def interrupt_result(*args: Any, **kwargs: Any) -> Any:
            if not started.wait(3):
                raise RuntimeError("Test capture did not start")
            raise KeyboardInterrupt

        rows = [report.Row(f"r{i}", f"https://example.com/{i}") for i in range(10)]
        with patch.object(report, "ThreadPoolExecutor", GatedExecutor):
            if during_submit:
                with self.assertRaises(KeyboardInterrupt):
                    report.run_batch(rows, self.root, GatedClient)
            else:
                with (
                    patch.object(Future, "result", side_effect=interrupt_result),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    report.run_batch(rows, self.root, GatedClient)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.root.glob("run-*/manifest.json")), [])

    def test_failed_write_does_not_publish_partial_png(self) -> None:
        client = FakeClient()
        with patch.object(Path, "replace", side_effect=OSError("secret-value")):
            run = report.run_batch(
                [report.Row("a", "https://example.com/")], self.root, lambda: client
            )
        data = json.loads((run.directory / "manifest.json").read_text())
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(data["rows"][0]["error"], "local_file_error")
        self.assertFalse((run.directory / "images/a.png").exists())
        self.assertNotIn("secret-value", json.dumps(data))

    def test_key_in_input_is_rejected_without_leaking_or_calling(self) -> None:
        path = self.csv("id,url\na,https://example.com/test-environment-key\n")
        for key in ("test-environment-key", " test-environment-key\n"):
            with self.subTest(key=repr(key)):
                errors = StringIO()
                with (
                    patch.dict(os.environ, {"SITESHOT_API_KEY": key}),
                    patch.object(report, "SiteShot") as client,
                    redirect_stderr(errors),
                ):
                    code = report.main([str(path), "--output", str(self.root)])
                self.assertEqual(code, 2)
                client.assert_not_called()
                self.assertNotIn("test-environment-key", errors.getvalue())

    def test_whitespace_only_key_is_rejected_before_creating_report(self) -> None:
        path = self.csv("id,url\na,https://example.com/\n")
        errors = StringIO()
        with (
            patch.dict(os.environ, {"SITESHOT_API_KEY": " \t\n"}),
            patch.object(report, "SiteShot") as client,
            redirect_stderr(errors),
        ):
            code = report.main([str(path), "--output", str(self.root / "reports")])
        self.assertEqual(code, 2)
        client.assert_not_called()
        self.assertIn("no requests were sent", errors.getvalue())
        self.assertFalse((self.root / "reports").exists())

    def test_cli_env_key_fixed_budget_and_exit(self) -> None:
        path = self.csv("id,url\na,https://example.com/\n")
        client = FakeClient(APIError("sensitive response body"))
        output, errors = StringIO(), StringIO()
        with (
            patch.dict(os.environ, {"SITESHOT_API_KEY": "test-environment-key"}),
            patch.object(report, "SiteShot", return_value=client) as factory,
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = report.main([str(path), "--output", str(self.root)])
        self.assertEqual(code, 1)
        factory.assert_called_once_with("test-environment-key", timeout=90, retries=0)
        self.assertNotIn("test-environment-key", output.getvalue() + errors.getvalue())
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(report, "SiteShot") as factory,
            redirect_stderr(errors),
        ):
            self.assertEqual(report.main([str(path)]), 2)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
