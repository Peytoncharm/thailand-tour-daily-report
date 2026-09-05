"""Stale tracking-link guard for driver_location.py (5 Sep 2026 incident).

Opening the driver GPS link for a job that is already over must not create a
session, must not start the 5-minute watchdog, and must never push
"คนขับเปิดลิงก์แต่ยังไม่ได้แชร์ตำแหน่ง" to the driver.

Runs offline with the stdlib + flask:
    python -m unittest test_driver_location_stale -v
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from flask import Flask

import driver_location as dl


NOW = datetime(2026, 9, 5, 11, 0, tzinfo=dl.ICT)


class JobIsOverTests(unittest.TestCase):

    def test_real_incident_bookings_are_over(self):
        # Peter k Kenyon — pickup 20 May, still "Confirmed" when it re-fired on 3 Sep
        self.assertTrue(dl.job_is_over("2026-05-20T08:00:00+07:00", "Confirmed", NOW))
        # Grzegorz Kuć — pickup 15 Aug, already "Completed" when it re-fired on 24 Aug
        self.assertTrue(dl.job_is_over("2026-08-15T08:00:00+07:00", "Completed", NOW))

    def test_closed_status_is_over_regardless_of_date(self):
        future = (NOW + timedelta(days=2)).isoformat()
        for status in ["Completed", "Refunded", "Rejected", "cancelled"]:
            with self.subTest(status=status):
                self.assertTrue(dl.job_is_over(future, status, NOW))

    def test_live_jobs_are_not_over(self):
        self.assertFalse(dl.job_is_over((NOW + timedelta(hours=6)).isoformat(), "Confirmed", NOW))
        self.assertFalse(dl.job_is_over((NOW - timedelta(hours=1)).isoformat(), "Confirmed", NOW))  # running late
        self.assertFalse(dl.job_is_over((NOW - timedelta(hours=5, minutes=59)).isoformat(), "Confirmed", NOW))

    def test_just_past_grace_window_is_over(self):
        self.assertTrue(dl.job_is_over((NOW - timedelta(hours=6, minutes=1)).isoformat(), "Confirmed", NOW))

    def test_unknown_pickup_fails_open(self):
        self.assertFalse(dl.job_is_over("", "Confirmed", NOW))
        self.assertFalse(dl.job_is_over(None, None, NOW))
        self.assertFalse(dl.job_is_over("not-a-date", "Confirmed", NOW))


class SharePageStaleLinkTests(unittest.TestCase):

    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(dl.driver_bp)
        self.client = app.test_client()
        dl.tracking_sessions.clear()

    def _open(self, info):
        with mock.patch.object(dl, "_fetch_booking_and_provider", return_value=info), \
             mock.patch.object(dl.threading, "Timer") as timer, \
             mock.patch.object(dl, "render_template", return_value="SHARE PAGE"):
            resp = self.client.get("/driver/track/464930000006641045")
        return resp, timer

    def test_stale_link_no_session_no_timer(self):
        resp, timer = self._open({
            "customer_name": "Peter k Kenyon", "pickup_time": "08:00",
            "pickup_datetime_iso": "2026-05-20T08:00:00+07:00", "status": "Confirmed",
            "line_user_id": "Uabc", "provider_id": "p1", "type_of_package": "Private Transfer",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("จบไปแล้ว", resp.get_data(as_text=True))
        self.assertNotIn("464930000006641045", dl.tracking_sessions)
        timer.assert_not_called()

    def test_live_link_still_creates_session_and_timer(self):
        soon = (datetime.now(dl.ICT) + timedelta(hours=5)).isoformat()
        resp, timer = self._open({
            "customer_name": "Live Customer", "pickup_time": "16:00",
            "pickup_datetime_iso": soon, "status": "Confirmed",
            "line_user_id": "Uabc", "provider_id": "p1", "type_of_package": "Private Transfer",
        })
        self.assertEqual(resp.get_data(as_text=True), "SHARE PAGE")
        self.assertIn("464930000006641045", dl.tracking_sessions)
        timer.assert_called_once()


class WatchdogGuardTests(unittest.TestCase):

    def setUp(self):
        dl.tracking_sessions.clear()

    def _session(self, **over):
        s = {
            "started_at": None, "watchdog_fired": False,
            "customer_name": "Peter k Kenyon", "pickup_time": "08:00",
            "line_user_id": "Uabc", "provider_id": "p1",
            "type_of_package": "Private Transfer", "provider_name": "Driver",
            "pickup_datetime_iso": "2026-05-20T08:00:00+07:00", "status": "Confirmed",
        }
        s.update(over)
        dl.tracking_sessions["b1"] = s
        return s

    def test_watchdog_never_nudges_for_finished_job(self):
        s = self._session()
        with mock.patch.object(dl, "_line_push") as push, \
             mock.patch.object(dl, "should_block", return_value=(False, "")):
            dl._watchdog_check("b1")
        push.assert_not_called()
        self.assertTrue(s["watchdog_fired"])   # and never re-tries

    def test_watchdog_still_nudges_for_live_job(self):
        soon = (datetime.now(dl.ICT) + timedelta(hours=5)).isoformat()
        self._session(pickup_datetime_iso=soon)
        with mock.patch.object(dl, "_line_push") as push, \
             mock.patch.object(dl, "should_block", return_value=(False, "")):
            dl._watchdog_check("b1")
        push.assert_called_once()
        self.assertIn("ยังไม่ได้แชร์", push.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
