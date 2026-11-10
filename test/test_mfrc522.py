#!/usr/bin/env python3
import contextlib
import pathlib
import sys
import unittest
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.modules.setdefault("bus", types.SimpleNamespace())
sys.path.insert(0, str(ROOT / "src"))
import mfrc522


class FakeSpi:
    def __init__(self):
        self.sent = []
        self.response = b"\x00\x92"

    def spi_transfer(self, data):
        self.sent.append(("transfer", list(data)))
        return {"response": self.response}

    def spi_send(self, data):
        self.sent.append(("send", list(data)))


def test_register_framing_and_crc():
    spi = FakeSpi()
    reader = mfrc522.Mfrc522(spi)

    assert reader.reg_read(reader.VERSION) == 0x92
    assert spi.sent[-1] == ("transfer", [0xEE, 0x00])

    reader.reg_write(reader.TX_CONTROL, 0x03)
    assert spi.sent[-1] == ("send", [0x28, 0x03])

    # ISO14443-A CRC for a MIFARE READ of block 4, little-endian on the wire.
    assert reader._calculate_crc([0x30, 0x04]) == [0x26, 0xEE]
    assert reader.reader_power(True) is None


class FakeGcmd:
    def __init__(self, params):
        self.params = params
        self.responses = []

    def get_int(self, name, default, **kwargs):
        return self.params.get(name, default)

    def error(self, message):
        return RuntimeError(message)

    def respond_info(self, message):
        self.responses.append(message)


def test_oamsm_rfid_read_command():
    calls = []
    reader = types.SimpleNamespace(
        oams_index=2, rfid_card=1,
        read_now=lambda: calls.append("read"),
        format_last_read=lambda: (
            "OAMSM_RFID_READ OAMS=2 RFID_CARD=1 STATUS=PRESENT "
            "LAST_READ_STATUS=PRESENT UID=01020304 BLOCK=NONE "
            "VERSION=0x92 ERROR=NONE"))
    registry = mfrc522.OpenAmsRfidRegistry.__new__(
        mfrc522.OpenAmsRfidRegistry)
    registry.readers = {(2, 1): reader}
    gcmd = FakeGcmd({"OAMS": 2, "RFID_CARD": 1})

    registry.cmd_RFID_READ(gcmd)

    assert calls == ["read"]
    assert "LAST_READ_STATUS=PRESENT" in gcmd.responses[0]
    assert "UID=01020304" in gcmd.responses[0]


def test_ready_serializes_dual_reader_drivers():
    callbacks = []
    timers = []
    operations = []
    reactor = types.SimpleNamespace(
        register_callback=lambda cb: callbacks.append(cb),
        register_timer=lambda cb, when: timers.append((cb, when)),
        monotonic=lambda: 20.0)
    poll_a = lambda eventtime: eventtime
    poll_b = lambda eventtime: eventtime

    def fake_driver(name, poll):
        return types.SimpleNamespace(
            _initialize=lambda eventtime: operations.append(
                (name, "initialize", eventtime)),
            _poll=poll, poll_interval=0.5)

    driver_a = fake_driver("a", poll_a)
    driver_b = fake_driver("b", poll_b)
    registry = mfrc522.OpenAmsRfidRegistry.__new__(
        mfrc522.OpenAmsRfidRegistry)
    registry.reactor = reactor
    registry.drivers = {1: driver_a, 2: driver_b}

    registry._handle_ready()
    assert operations == []
    assert len(callbacks) == 1

    callbacks[0](10.0)
    assert operations == [
        ("a", "initialize", 10.0),
        ("b", "initialize", 10.0)]
    assert timers == [(poll_a, 20.5), (poll_b, 20.5)]


def test_soft_reset_timeout_reports_command_register():
    reader = mfrc522.Mfrc522(FakeSpi())
    reader.reg_write = lambda reg, value: None
    reader.reg_read = lambda reg: 0x10

    try:
        reader.initialize()
    except mfrc522.Mfrc522Error as exc:
        assert str(exc) == "soft reset timed out (CommandReg=0x10)"
    else:
        raise AssertionError("soft reset timeout was not reported")


def test_fm17580_production_versions_are_accepted():
    for production_version in (0xA1, 0xEE):
        reader = mfrc522.Mfrc522(FakeSpi())
        writes = []
        reader.reg_write = lambda reg, value: writes.append((reg, value))
        reader.reg_read = lambda reg, version=production_version: (
            version if reg == reader.VERSION else 0)

        assert reader.initialize() == production_version
        assert (reader.COMMAND, reader.CMD_SOFT_RESET) in writes


def test_one_driver_owns_and_serializes_both_readers():
    class PairSpi:
        def __init__(self, mcu, queue):
            self.mcu = mcu
            self.queue = queue

        def get_mcu(self):
            return self.mcu

        def get_command_queue(self):
            return self.queue

    class ResetPin:
        def __init__(self):
            self.start_values = []

        def setup_start_value(self, value, shutdown_value):
            self.start_values.append((value, shutdown_value))

    common_mcu = object()
    spis = [PairSpi(common_mcu, "queue-a"),
            PairSpi(common_mcu, "queue-b")]
    spi_options = []
    original_spi_factory = getattr(
        mfrc522.bus, "MCU_SPI_from_config", None)

    def make_spi(config, **kwargs):
        spi_options.append(kwargs["pin_option"])
        return spis[len(spi_options) - 1]

    mux_readers = []
    gcode = types.SimpleNamespace(
        register_command=lambda *args, **kwargs: None,
        register_mux_command=lambda command, key, value, callback, **kwargs:
            mux_readers.append(value))
    reactor = types.SimpleNamespace(monotonic=lambda: 100.0,
                                    mutex=contextlib.nullcontext)
    reset_pins = {}
    pins = types.SimpleNamespace(
        setup_pin=lambda pin_type, name: reset_pins.setdefault(
            name, ResetPin()))
    objects = {"gcode": gcode, "pins": pins}
    printer = types.SimpleNamespace(
        get_reactor=lambda: reactor,
        lookup_object=lambda name, default=None: objects.get(name, default),
        add_object=lambda name, value: objects.__setitem__(name, value),
        register_event_handler=lambda *args: None)
    values = {
        "oams": 1,
        "cs_pin_a": "oams_mcu1:PB3",
        "reset_pin_a": "oams_mcu1:PD2",
        "cs_pin_b": "oams_mcu1:PB2",
        "reset_pin_b": "oams_mcu1:PB0",
        "spi_speed": 100000,
    }
    config = types.SimpleNamespace(
        get_printer=lambda: printer,
        get_name=lambda: "mfrc522 openams",
        getint=lambda name, default=None, **kwargs: values.get(name, default),
        getfloat=lambda name, default=None, **kwargs: values.get(name, default),
        getboolean=lambda name, default=None, **kwargs: values.get(name, default),
        get=lambda name, default=None: values.get(name, default),
        error=lambda message: RuntimeError(message))

    try:
        mfrc522.bus.MCU_SPI_from_config = make_spi
        driver = mfrc522.OpenAmsMfrc522Pair(config)
    finally:
        if original_spi_factory is None:
            del mfrc522.bus.MCU_SPI_from_config
        else:
            mfrc522.bus.MCU_SPI_from_config = original_spi_factory

    assert spi_options == ["cs_pin_a", "cs_pin_b"]
    assert [reader.rfid_card for reader in driver.readers] == [0, 1]
    assert mux_readers == ["rfid_a", "rfid_b"]
    assert [reader.spi for reader in driver.readers] == spis
    assert reset_pins["oams_mcu1:PD2"].start_values == [(1, 1)]
    assert reset_pins["oams_mcu1:PB0"].start_values == [(1, 1)]
    registry = objects["oamsm_rfid_registry"]
    assert registry.drivers == {1: driver}
    assert sorted(registry.readers) == [(1, 0), (1, 1)]

    field_operations = []
    for reader in driver.readers:
        card = reader.rfid_card
        reader.reader.initialize = lambda card=card: (
            field_operations.append(("init", card)) or (0x91 + card))
        reader.reader.antenna_on = lambda card=card: (
            field_operations.append(("on", card)))
        reader.reader.antenna_off = lambda card=card: (
            field_operations.append(("off", card)))
    driver._initialize(9.0)
    assert field_operations == [("init", 0), ("off", 0), ("init", 1)]
    assert driver.active_reader == 1
    assert [reader.version for reader in driver.readers] == [0x91, 0x92]

    # Switching readers disables the old RF field before enabling the new one.
    field_operations[:] = []
    assert driver.activate_reader(driver.readers[0]) == 0x91
    assert field_operations == [("off", 1), ("on", 0)]
    assert driver.active_reader == 0

    # Reusing the active reader does not touch CS, reset, or its RF field.
    assert driver.activate_reader(driver.readers[0]) == 0x91
    assert field_operations == [("off", 1), ("on", 0)]

    operations = []
    for reader in driver.readers:
        card = reader.rfid_card
        reader._sample = lambda eventtime, card=card: (
            operations.append((card, eventtime)))
    # Both readers in turn; the next poll is poll_interval after the
    # scheduled time, or after now when the samples ran late (this test's
    # reactor clock reads 100.0).
    assert driver._poll(10.0) == 100.5
    assert operations == [(0, 10.0), (1, 10.0)]
    # Nothing left of the timing rule (design 7.1).
    assert not hasattr(driver, "_pick_bay")
    assert not hasattr(driver, "_bay_rise_times")


def test_firmware_debug_decodes_shared_bus_state():
    # PB0 (B NPD), PB2 (B CS), and PB3 (A CS) are outputs and high.
    b_moder = (1 << 0) | (1 << 4) | (1 << 6)
    b_levels = (0x000D << 16) | 0x000D
    d_moder = 1 << 4
    d_levels = (0x0004 << 16) | 0x0004

    def spi_config(oid, encoded_cs):
        return (oid | (encoded_cs << 8) | (1 << 16) | (1 << 17)
                | (480 << 21))

    def spi_last(tx, rx, bus_index):
        return (tx | (0xFF << 8) | (rx << 16) | (1 << 25)
                | (1 << 26) | (bus_index << 31))

    params = {
        "gpiob_moder": b_moder, "gpiob_levels": b_levels,
        "gpiod_moder": d_moder, "gpiod_levels": d_levels,
        "spi0_config": spi_config(3, 0x13),
        "spi0_last": spi_last(0xEE, 0xA1, 0),
        "spi1_config": spi_config(5, 0x12),
        "spi1_last": spi_last(0x82, 0xFF, 0),
    }
    report = mfrc522.OpenAmsMfrc522._format_firmware_debug(params)
    assert "PB0(mode=1 in=1 out=1)" in report
    assert "PB2(mode=1 in=1 out=1)" in report
    assert "PB3(mode=1 in=1 out=1)" in report
    assert "PD2(mode=1 in=1 out=1)" in report
    assert "spi0(oid=3 cs=PB3 configured=1 active_high=0" in report
    assert "spi1(oid=5 cs=PB2 configured=1 active_high=0" in report
    assert "period_ticks=480" in report
    assert "tx0=0x82 rx0=0xff rx_last=0xff" in report
    assert "selected_cs=0 idle_cs=1 other_cs_idle=1 bus=0" in report

    sends = []
    command = types.SimpleNamespace(
        send=lambda args: sends.append(args) or params)
    mcu = types.SimpleNamespace(lookup_query_command=lambda *args, **kwargs: command)
    obj = mfrc522.OpenAmsMfrc522.__new__(mfrc522.OpenAmsMfrc522)
    obj.name = "rfid_b"
    obj._firmware_debug_reported = False
    obj.spi = types.SimpleNamespace(
        get_mcu=lambda: mcu, get_command_queue=lambda: "rfid_queue")
    obj._log_firmware_debug()
    obj._log_firmware_debug()
    assert sends == [[]]


class SimTag:
    """One ISO/IEC 14443-A tag in a simulated field."""

    def __init__(self, uid, sak=0x08):
        self.uid = bytes(uid)
        self.sak = sak
        self.state = "IDLE"
        self.level = 0
        if len(self.uid) == 4:
            parts = [list(self.uid)]
        elif len(self.uid) == 7:
            parts = [[0x88] + list(self.uid[0:3]), list(self.uid[3:7])]
        else:
            parts = [[0x88] + list(self.uid[0:3]),
                     [0x88] + list(self.uid[3:6]), list(self.uid[6:10])]
        self.parts = [p + [p[0] ^ p[1] ^ p[2] ^ p[3]] for p in parts]

    def bits(self, level):
        return mfrc522._bytes_to_bits(self.parts[level])


class SimChip(mfrc522.Mfrc522):
    """An MFRC522 whose transceive answers from simulated tags: REQA, WUPA,
    bit-wise anticollision with the chip's collision position, SELECT and
    HLTA. ``convention`` is how CollPos counts once known bits were sent
    (the open hardware question, design G4)."""

    LEVELS = {0x93: 0, 0x95: 1, 0x97: 2}

    def __init__(self, tags, convention="absolute"):
        super().__init__(spi=None)
        self.tags = list(tags)
        self.convention = convention
        self.coll = 0

    def reg_write(self, reg, value):
        pass

    def reg_read(self, reg):
        return 0

    def _communicate(self, command, send_data, tx_last_bits=0, rx_align=0,
                     check_crc=False, collision=False):
        data = list(send_data)
        if data[0] in (0x26, 0x52) and tx_last_bits == 7:
            woken = ("IDLE",) if data[0] == 0x26 else ("IDLE", "HALT")
            answering = [t for t in self.tags if t.state in woken]
            if not answering:
                raise mfrc522.Mfrc522NoCard("no card")
            for tag in answering:
                tag.state, tag.level = "READY", 0
            return [0x04, 0x00], 16
        if data[0] == 0x50:
            for tag in self.tags:
                if tag.state == "ACTIVE":
                    tag.state = "HALT"
            raise mfrc522.Mfrc522NoCard("no answer")
        level = self.LEVELS[data[0]]
        ready = [t for t in self.tags
                 if t.state == "READY" and t.level == level]
        if data[1] == 0x70:
            part = data[2:7]
            chosen = [t for t in ready if t.parts[level] == part]
            for tag in ready:
                if tag not in chosen:
                    tag.state = "IDLE"
            if not chosen:
                raise mfrc522.Mfrc522NoCard("no card")
            # Tags sharing this cascade level answer one identical SAK.
            for tag in chosen:
                if part[0] == 0x88:
                    tag.level = level + 1
                else:
                    tag.state = "ACTIVE"
            sak = 0x04 if part[0] == 0x88 else chosen[0].sak
            frame = [sak]
            return frame + self._calculate_crc(frame), 24
        nbits = ((data[1] >> 4) - 2) * 8 + (data[1] & 7)
        known = mfrc522._bytes_to_bits(data[2:])[:nbits]
        answering = [t for t in ready if t.bits(level)[:nbits] == known]
        if not answering:
            raise mfrc522.Mfrc522NoCard("no card")
        rests = [t.bits(level)[nbits:40] for t in answering]
        index = None
        for i in range(40 - nbits):
            if len(set(rest[i] for rest in rests)) > 1:
                index = i
                break
        received = list(rests[0])
        if index is not None:
            received = received[:index] + [1] + [0] * (len(received)
                                                      - index - 1)
        fifo = [0] * rx_align + received
        response = list(mfrc522._bits_to_bytes(fifo))
        if not collision:
            return response, len(fifo)
        pos = None
        if index is not None:
            pos = {"absolute": nbits + index + 1,
                   "fifo": rx_align + index + 1,
                   "relative": index + 1}[self.convention]
            if pos > 32:
                raise mfrc522.Mfrc522Error("collision outside the UID bits")
            pos = pos % 32
            pos = pos or 32
        return response, len(fifo), pos


class ReadUidsTests(unittest.TestCase):
    """Mfrc522.read_uids: every tag in the field (design 7.1, test 15)."""

    def test_an_empty_field_is_empty(self):
        self.assertEqual(SimChip([]).read_uids(), [])

    def test_one_tag(self):
        uid = bytes.fromhex("11223344")
        self.assertEqual(SimChip([SimTag(uid)]).read_uids(), [uid])

    def test_two_tags_of_two_spools_both_answer(self):
        a, b = bytes.fromhex("11223344"), bytes.fromhex("11a23344")
        for convention in ("absolute", "fifo", "relative"):
            with self.subTest(convention=convention):
                chip = SimChip([SimTag(a), SimTag(b)], convention)
                self.assertEqual(sorted(chip.read_uids()), sorted([a, b]))
                self.assertEqual(chip.saks, {a: 0x08, b: 0x08})

    def test_a_collision_at_the_second_cascade_level(self):
        a = bytes.fromhex("04112233445566")
        b = bytes.fromhex("04112233c45566")
        chip = SimChip([SimTag(a, sak=0x00), SimTag(b, sak=0x00)])
        self.assertEqual(sorted(chip.read_uids()), sorted([a, b]))

    def test_three_tags_with_collisions_after_known_bits(self):
        uids = [bytes.fromhex(h) for h in ("10000000", "10000001",
                                           "30000001")]
        chip = SimChip([SimTag(u) for u in uids])
        self.assertEqual(sorted(chip.read_uids()), sorted(uids))

    def test_at_most_four_tags(self):
        uids = [bytes([0x20 + i, 1, 2, 3]) for i in range(5)]
        chip = SimChip([SimTag(u) for u in uids])
        self.assertEqual(len(chip.read_uids()), 4)

    def test_a_tag_lost_during_anticollision_is_an_error_not_empty(self):
        tag = SimTag(bytes.fromhex("11223344"))
        chip = SimChip([tag])
        real = chip._communicate

        def leave(command, send_data, *args, **kwargs):
            if send_data[0] == 0x93 and send_data[1] != 0x70:
                chip.tags = []
            return real(command, send_data, *args, **kwargs)

        chip._communicate = leave
        with self.assertRaises(mfrc522.Mfrc522Error) as caught:
            chip.read_uids()
        self.assertNotIsInstance(caught.exception, mfrc522.Mfrc522NoCard)

    def test_select_uid_wakes_and_selects_one_tag(self):
        a, b = bytes.fromhex("11223344"), bytes.fromhex("55667788")
        tags = [SimTag(a), SimTag(b, sak=0x18)]
        chip = SimChip(tags)
        chip.read_uids()
        self.assertEqual(chip.select_uid(b), 0x18)
        self.assertEqual([t.state for t in tags], ["IDLE", "ACTIVE"])


class FakeMutex(object):
    """Klipper's reactor mutex, as far as a test can tell: entering it while
    it is held is a sample that would have to wait."""

    def __init__(self):
        self.locked = False

    def __enter__(self):
        if self.locked:
            raise AssertionError("a second sample while one is running")
        self.locked = True
        return self

    def __exit__(self, *exc):
        self.locked = False
        return False


class FieldHandoffTests(unittest.TestCase):
    """[mfrc522] hands whole samples to the unit's TagReader."""

    def _pair(self):
        notes = []
        tag_reader = types.SimpleNamespace(
            note_field=lambda reader, uids, now, records=None: notes.append(
                (reader, uids, records)),
            attach_readers=lambda source, coverage, **options: notes.append(
                ("attach", coverage, sorted(options))),
            focus_readers=lambda now: [],
            needs_record=lambda uid: True,
            reader_at_rest=lambda reader: True,
            note_decode_failed=lambda uid: None,
            pending_record=lambda uid: {"format": "bambu", "bambu": None},
            record_for_key=lambda key: None)
        oams = types.SimpleNamespace(oams_idx=1, tag_reader=tag_reader)
        pair = mfrc522.OpenAmsMfrc522Pair.__new__(mfrc522.OpenAmsMfrc522Pair)
        pair.printer = types.SimpleNamespace(
            lookup_objects=lambda module=None: [("oams oams1", oams)],
            send_event=lambda event, *args: pair._handle_field(*args))
        pair.reactor = types.SimpleNamespace(monotonic=lambda: 5.0)
        pair._mutex = FakeMutex()
        pair.oams_index = 1
        pair.bays = {0: (0, 1), 1: (2, 3)}
        pair.attribution_options = {"debounce": 2}
        pair._attached = None
        pair.active_reader = None
        pair.poll_interval = 0.5
        pair.focus_poll_interval = 0.1
        pair.tag_processor = None
        endpoint = mfrc522.OpenAmsMfrc522.__new__(mfrc522.OpenAmsMfrc522)
        endpoint.owner = pair
        endpoint.printer = pair.printer
        endpoint.reactor = pair.reactor
        endpoint.name = "rfid_a"
        endpoint.rfid_card = 0
        endpoint.version = 0x92
        endpoint.uids = []
        endpoint.uid = None
        endpoint.records = {}
        endpoint.block_data = None
        endpoint.tag_data = None
        endpoint.tag_processor = None
        endpoint.read_block_number = None
        endpoint._decode_failed = {}
        endpoint.last_error = None
        endpoint.last_read_status = "NOT_READ"
        pair.readers = (endpoint,)
        pair.activate_reader = lambda reader, force=False: 0x92
        return pair, endpoint, notes

    def test_every_uid_goes_in_one_sample(self):
        pair, endpoint, notes = self._pair()
        a, b = bytes.fromhex("11223344"), bytes.fromhex("11a23344")
        endpoint.reader = SimChip([SimTag(a), SimTag(b)])
        self.assertEqual(sorted(pair.sample_reader(0)),
                         sorted([a.hex(), b.hex()]))
        self.assertEqual(notes[0], ("attach", [[0, 1], [2, 3]],
                                    ["debounce"]))
        reader, uids, records = notes[-1]
        self.assertEqual((reader, sorted(uids)), (0, sorted([a.hex(),
                                                             b.hex()])))
        self.assertEqual(endpoint.last_read_status, "PRESENT")
        self.assertEqual(len(endpoint.uids), 2)

    def test_a_sample_holds_the_pairs_mutex(self):
        # Review 2026-11-10, finding 12: the poll yields at every SPI round
        # trip; a command's sample starting meanwhile switched the antenna
        # to the other reader under it. One mutex serialises the pair.
        pair, endpoint, notes = self._pair()
        held = []

        class Watched(SimChip):
            def read_uids(self, max_tags=None):
                held.append(pair._mutex.locked)
                return SimChip.read_uids(self, max_tags)

        endpoint.reader = Watched([SimTag(bytes.fromhex("11223344"))])
        pair.sample_reader(0)
        self.assertEqual(held, [True])
        self.assertFalse(pair._mutex.locked)
        with pair._mutex:                  # a sample is running
            with self.assertRaises(AssertionError):
                pair.sample_reader(0)      # this one would wait

    def test_an_explicit_read_retries_a_failed_decode_now(self):
        # Review 2026-11-10, finding 11: not 10 s after the last failure.
        pair, endpoint, notes = self._pair()
        uid = bytes.fromhex("11223344")
        decoded = []
        endpoint.tag_processor = types.SimpleNamespace(
            read_tag=lambda reader, tag_uid: decoded.append(tag_uid) or {
                "tray_uid": "ab" * 16})
        endpoint._decode_failed = {uid.hex(): 4.0}   # failed 1 s ago
        endpoint.reader = SimChip([SimTag(uid)])
        pair.readers[0]._sample(5.0)                 # the poll waits
        self.assertEqual(decoded, [])
        pair.sample_reader(0)                        # OAMS_RFID_SCAN BAY=
        self.assertEqual(decoded, [uid])
        self.assertEqual(notes[-1][2][uid.hex()]["format"], "bambu")

    def test_an_error_is_an_error_not_a_removal(self):
        # Was: a collision (bad BCC) counted as a miss, and two misses
        # reported the tag removed, which cleared the bay's tag.
        pair, endpoint, notes = self._pair()

        class Broken(SimChip):
            def read_uids(self, max_tags=None):
                raise mfrc522.Mfrc522Error("UID BCC mismatch")

        endpoint.reader = Broken([])
        self.assertIsNone(pair.sample_reader(0))
        self.assertEqual(notes[-1], (0, None, {}))
        self.assertEqual(endpoint.last_read_status, "ERROR")

    def test_a_mifare_tag_seen_without_a_processor_has_no_record(self):
        pair, endpoint, notes = self._pair()
        endpoint.reader = SimChip([SimTag(bytes.fromhex("11223344"))])
        pair.sample_reader(0)
        self.assertEqual(notes[-1][2], {})

    def test_a_mifare_tag_not_decoded_yet_is_a_pending_bambu_tag(self):
        pair, endpoint, notes = self._pair()
        endpoint.tag_processor = object()      # configured, not at rest
        pair.tag_reader().reader_at_rest = lambda reader: False
        endpoint.reader = SimChip([SimTag(bytes.fromhex("11223344"))])
        pair.sample_reader(0)
        self.assertEqual(notes[-1][2],
                         {"11223344": {"format": "bambu", "bambu": None}})


def load_tests(loader, tests, pattern):
    del tests, pattern
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(ReadUidsTests),
        loader.loadTestsFromTestCase(FieldHandoffTests),
        unittest.FunctionTestCase(test_register_framing_and_crc),
        unittest.FunctionTestCase(test_oamsm_rfid_read_command),
        unittest.FunctionTestCase(test_ready_serializes_dual_reader_drivers),
        unittest.FunctionTestCase(test_soft_reset_timeout_reports_command_register),
        unittest.FunctionTestCase(test_fm17580_production_versions_are_accepted),
        unittest.FunctionTestCase(test_one_driver_owns_and_serializes_both_readers),
        unittest.FunctionTestCase(test_firmware_debug_decodes_shared_bus_state),
    ])


if __name__ == "__main__":
    test_register_framing_and_crc()
    test_oamsm_rfid_read_command()
    test_ready_serializes_dual_reader_drivers()
    test_soft_reset_timeout_reports_command_register()
    test_fm17580_production_versions_are_accepted()
    test_one_driver_owns_and_serializes_both_readers()
    test_firmware_debug_decodes_shared_bus_state()
    print("PASS: MFRC522/FM17580 initialization and RFID command")
