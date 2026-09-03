import os
import unittest
from unittest.mock import patch

import pytz

from app.config import _read_bounded_int
from app.main import (
    _add_email_automation_jobs,
    _scheduled_email_automation_job,
)


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, function, **kwargs):
        self.jobs[kwargs["id"]] = {"function": function, **kwargs}


class EmailScheduleTests(unittest.TestCase):
    def test_registers_only_the_morning_email_job(self):
        scheduler = FakeScheduler()
        timezone = pytz.timezone("Europe/Berlin")

        _add_email_automation_jobs(scheduler, timezone)

        self.assertEqual(
            set(scheduler.jobs),
            {"daily_email_automation"},
        )
        morning = scheduler.jobs["daily_email_automation"]
        self.assertIs(morning["function"], _scheduled_email_automation_job)
        self.assertEqual(str(morning["trigger"].fields[5]), "5")
        self.assertEqual(str(morning["trigger"].fields[6]), "0")
        self.assertEqual(str(morning["trigger"].timezone), "Europe/Berlin")
        self.assertEqual(morning["max_instances"], 1)

    def test_schedule_value_must_be_an_integer(self):
        with patch.dict(os.environ, {"TEST_SCHEDULE_VALUE": "noon"}):
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                _read_bounded_int("TEST_SCHEDULE_VALUE", 12, 0, 23)

    def test_schedule_value_must_be_in_range(self):
        with patch.dict(os.environ, {"TEST_SCHEDULE_VALUE": "24"}):
            with self.assertRaisesRegex(ValueError, "must be between 0 and 23"):
                _read_bounded_int("TEST_SCHEDULE_VALUE", 12, 0, 23)


if __name__ == "__main__":
    unittest.main()
