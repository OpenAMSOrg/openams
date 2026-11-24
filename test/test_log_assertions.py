"""assert_no_logs (test/log_assertions.py): unittest's assertNoLogs on every
Python the plugin supports, 3.8 included.

    python3 -m unittest discover -s test -p 'test_log_assertions.py' -v
"""

import logging
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from log_assertions import assert_no_logs   # noqa: E402


class AssertNoLogsTest(unittest.TestCase):
    def test_a_quiet_block_passes(self):
        with assert_no_logs(self, level="WARNING"):
            logging.getLogger("openams.test").info("below the level")

    def test_a_record_at_the_level_fails(self):
        with self.assertRaises(AssertionError) as caught:
            with assert_no_logs(self, level="WARNING"):
                logging.getLogger("openams.test").warning("the unit is %s",
                                                          "late")
        self.assertIn("WARNING:openams.test:the unit is late",
                      str(caught.exception))

    def test_a_named_logger_and_a_numeric_level(self):
        with assert_no_logs(self, "openams.test", logging.ERROR):
            logging.getLogger("openams.other").error("another logger")
            logging.getLogger("openams.test").warning("below the level")
        with self.assertRaises(AssertionError):
            with assert_no_logs(self, "openams.test", logging.ERROR):
                logging.getLogger("openams.test.child").error("a child")

    def test_the_logger_is_put_back(self):
        root = logging.getLogger()
        before = (root.handlers[:], root.level, root.propagate)
        with self.assertRaises(AssertionError):
            with assert_no_logs(self, level="DEBUG"):
                logging.debug("caught")
        self.assertEqual((root.handlers, root.level, root.propagate), before)

    def test_an_error_in_the_block_goes_through(self):
        with self.assertRaises(KeyError):
            with assert_no_logs(self):
                raise KeyError("x")


if __name__ == "__main__":
    unittest.main()
