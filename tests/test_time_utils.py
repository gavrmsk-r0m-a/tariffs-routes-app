import unittest
from datetime import datetime, timezone

from app.time_utils import (
    app_date_bound_to_utc,
    app_input_to_utc,
    app_now_input_value,
    format_app_datetime,
)


class OperationalTimeTest(unittest.TestCase):
    def test_utc_timestamp_is_rendered_in_gmt_plus_3(self):
        self.assertEqual(
            format_app_datetime("2026-10-20T22:10:00+00:00", with_timezone=True),
            "2026-10-21 01:10 (GMT+3)",
        )

    def test_datetime_local_input_is_interpreted_as_gmt_plus_3(self):
        self.assertEqual(
            app_input_to_utc("2026-10-21T01:10"),
            "2026-10-20T22:10:00+00:00",
        )

    def test_local_date_filter_bounds_are_converted_to_utc(self):
        self.assertEqual(
            app_date_bound_to_utc("2026-10-21"),
            "2026-10-20T21:00:00+00:00",
        )
        self.assertEqual(
            app_date_bound_to_utc("2026-10-21", end_of_day=True),
            "2026-10-21T20:59:59.999999+00:00",
        )

    def test_form_default_uses_gmt_plus_3(self):
        self.assertEqual(
            app_now_input_value(datetime(2026, 10, 20, 22, 10, tzinfo=timezone.utc)),
            "2026-10-21T01:10",
        )


if __name__ == "__main__":
    unittest.main()
