import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import app
from app import LocalCampusClient, UbaaClient, _parse_time, _time_range


class ParsingTests(unittest.TestCase):
    def test_render_scheduler_replaces_existing_timer(self):
        dashboard = Mock()
        dashboard._render_after_id = "old-timer"
        dashboard.after.return_value = "new-timer"
        dashboard._scheduled_render = Mock()

        app.Dashboard._schedule_render(dashboard)

        dashboard.after_cancel.assert_called_once_with("old-timer")
        dashboard.after.assert_called_once_with(60_000, dashboard._scheduled_render)
        self.assertEqual(dashboard._render_after_id, "new-timer")

    def test_parse_iso_time(self):
        self.assertEqual(_parse_time("2026-09-18 10:30") .hour, 10)

    def test_time_range_uses_given_day(self):
        day = datetime(2026, 9, 18, 12, 0)
        start, end = _time_range("10:00-11:35", day)
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (10, 0, 11, 35))

    def test_invalid_time_is_none(self):
        self.assertIsNone(_time_range("未定", datetime.now()))

    def test_unwrapped_today_response_is_supported(self):
        client = UbaaClient("https://example.invalid")
        client.request = lambda path: []
        snapshot = client.dashboard()
        self.assertEqual(snapshot.courses, [])

    def test_webvpn_url_wraps_upstream_host(self):
        url = LocalCampusClient("WEBVPN").upstream_url("https://byxt.buaa.edu.cn/example")
        self.assertIn("https://d.buaa.edu.cn/https/", url)

    def test_generic_captcha_reference_is_not_a_required_captcha(self):
        html = '<script src="/static/captcha-helper.js"></script>'
        self.assertIsNone(__import__("re").search(r"config\.captcha\s*=\s*\{\s*type\s*:", html, __import__("re").I))

    def test_portal_bootstrap_uses_current_user_probe(self):
        client = LocalCampusClient("DIRECT")
        response = Mock(status_code=200, text='{"code":"0"}', history=[], url=client.CURRENT_USER)
        response.raise_for_status.return_value = None
        client.session.get = Mock(return_value=response)

        client.bootstrap_portal()

        requested_url = client.session.get.call_args.args[0]
        self.assertEqual(requested_url, client.CURRENT_USER)
        self.assertNotEqual(requested_url, client.PORTAL)

    def test_credentials_are_dpapi_encrypted_and_round_trip(self):
        with TemporaryDirectory() as directory:
            original = app.CREDENTIAL_FILE
            app.CREDENTIAL_FILE = Path(directory) / "credentials.bin"
            try:
                app.save_credentials("test-user", "test-password")
                stored = app.CREDENTIAL_FILE.read_bytes()
                self.assertNotIn(b"test-user", stored)
                self.assertNotIn(b"test-password", stored)
                self.assertEqual(app.load_credentials(), ("test-user", "test-password"))
            finally:
                app.CREDENTIAL_FILE = original

    def test_window_position_round_trip(self):
        with TemporaryDirectory() as directory:
            original = app.UI_STATE_FILE
            app.UI_STATE_FILE = Path(directory) / "ui-state.json"
            try:
                app.save_window_position(-120, 245)
                self.assertEqual(app.load_window_position(), (-120, 245))
            finally:
                app.UI_STATE_FILE = original

    def test_transparent_background_persists_without_losing_position(self):
        with TemporaryDirectory() as directory:
            original = app.UI_STATE_FILE
            app.UI_STATE_FILE = Path(directory) / "ui-state.json"
            try:
                app.save_window_position(120, 245)
                app.save_transparent_background(True)
                self.assertTrue(app.load_transparent_background())
                self.assertEqual(app.load_window_position(), (120, 245))
                app.save_window_position(150, 275)
                self.assertTrue(app.load_transparent_background())
            finally:
                app.UI_STATE_FILE = original

    def test_judge_html_parsers_find_pending_assignment(self):
        courses = LocalCampusClient._judge_courses(
            '<a href="courselist.jsp?courseID=12"><b>程序设计</b></a>'
        )
        links = LocalCampusClient._judge_assignment_links(
            '<a href="assignment/index.jsp?assignID=34">第一次作业</a>'
        )
        pending = LocalCampusClient._judge_pending_from_detail(
            "作业时间：2026-09-01 08:00 至 2026-09-30 23:59 未提交答案",
            courses[0][1],
            links[0][1],
            datetime(2026, 9, 18),
        )
        self.assertEqual(courses, [("12", "程序设计")])
        self.assertEqual(links, [("34", "第一次作业")])
        self.assertIsNotNone(pending)
        self.assertEqual(pending.source, "希冀")

    def test_judge_submitted_assignment_is_filtered(self):
        pending = LocalCampusClient._judge_pending_from_detail(
            "作业时间：2026-09-01 08:00 至 2026-09-30 23:59 已提交",
            "程序设计",
            "第一次作业",
            datetime(2026, 9, 18),
        )
        self.assertIsNone(pending)


if __name__ == "__main__":
    unittest.main()
