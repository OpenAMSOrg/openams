"""H3 unit-driver side of the bay-spool links: config read, state, status.

The persisted options (spool_id_<bay>, link_source_<bay>) are read at config
time by OAMS._configure_spool_links; the manager commands that write them
(H3b) go through bay_link/set_bay_link, and the v2 status entry publishes the
pair per bay.
"""

import unittest

from test_ams_ht_driver import make_driver


class _ConfigError(Exception):
    """What a fake config.error(...) returns."""


class FakeConfig(object):
    """A config with just the option surface _configure_spool_links reads."""

    def __init__(self, **options):
        self.options = options

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getint(self, name, default=None, minval=None, maxval=None):
        value = self.options.get(name, default)
        value = None if value is None else int(value)
        if value is not None and minval is not None and value < minval:
            raise self.error("%s below minimum" % name)
        return value

    def error(self, message):
        return _ConfigError(message)


def linked_driver(**options):
    """A 4-bay driver with _configure_spool_links run over ``options``."""
    driver = make_driver()
    driver.bay_count = 4
    driver._configure_spool_links(FakeConfig(**options))
    return driver


def status_bays(driver):
    """The bays list the driver publishes, keyed by index."""
    return {entry["index"]: entry
            for entry in driver.get_webhook_status()["bays"]}


class SpoolLinkConfigTest(unittest.TestCase):
    def test_no_options_is_unlinked_everywhere(self):
        driver = linked_driver()
        self.assertEqual(driver.bay_links, [(None, "none")] * 4)
        for bay in range(4):
            self.assertEqual(driver.bay_link(bay), (None, "none"))
        bays = status_bays(driver)
        for bay in range(4):
            self.assertIsNone(bays[bay]["spool_id"])
            self.assertEqual(bays[bay]["link_source"], "none")

    def test_tag_link_and_manual_default(self):
        driver = linked_driver(spool_id_1=42, link_source_1="tag", spool_id_2=7)
        self.assertEqual(driver.bay_link(1), (42, "tag"))
        self.assertEqual(driver.bay_link(2), (7, "manual"))
        self.assertEqual(driver.bay_link(0), (None, "none"))
        bays = status_bays(driver)
        self.assertEqual(bays[1]["spool_id"], 42)
        self.assertEqual(bays[1]["link_source"], "tag")
        self.assertEqual(bays[2]["spool_id"], 7)
        self.assertEqual(bays[2]["link_source"], "manual")

    def test_link_source_without_spool_id_is_ignored(self):
        driver = linked_driver(link_source_3="bogus")
        self.assertEqual(driver.bay_link(3), (None, "none"))

    def test_bad_link_source_is_a_config_error(self):
        with self.assertRaises(_ConfigError):
            linked_driver(spool_id_0=3, link_source_0="bogus")

    def test_spool_id_below_one_is_rejected(self):
        with self.assertRaises(_ConfigError):
            linked_driver(spool_id_0=0)


class SetBayLinkTest(unittest.TestCase):
    def test_set_then_clear(self):
        driver = make_driver()
        driver.bay_count = 4
        driver.set_bay_link(2, 9, "manual")
        self.assertEqual(driver.bay_link(2), (9, "manual"))
        driver.set_bay_link(2, None, None)
        self.assertEqual(driver.bay_link(2), (None, "none"))

    def test_bad_source_raises_value_error(self):
        driver = make_driver()
        driver.bay_count = 4
        with self.assertRaises(ValueError):
            driver.set_bay_link(0, 5, "bogus")

    def test_bare_driver_has_no_links(self):
        driver = make_driver()
        self.assertEqual(driver.bay_link(0), (None, "none"))


if __name__ == "__main__":
    unittest.main()
