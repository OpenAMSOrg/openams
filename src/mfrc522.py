# OpenAMS dual MFRC522 reader over one Klipper software-SPI bus.
# Copyright (C) 2026 JR Lomas (discord:knight_rad.iant) <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


import json
import logging
import os
import re
import sys

# install.sh links each module into klippy/extras when it runs, and an update
# is only a git pull: a module a later release added has no link of its own, so
# the package also looks for siblings in this file's checkout (install.md).
if __package__:
    _CHECKOUT = os.path.dirname(os.path.realpath(__file__))
    if _CHECKOUT not in sys.modules[__package__].__path__:
        sys.modules[__package__].__path__.append(_CHECKOUT)

try:
    from . import bus
except (ImportError, ValueError):
    import bus

try:
    from . import oams_tags
    from . import tag_identity
except (ImportError, ValueError):
    import oams_tags
    import tag_identity


# A MIFARE Classic SAK: 1K (0x08, 0x88 on some clones) or 4K (0x18).
_MIFARE_SAKS = (0x08, 0x88, 0x18)
# A tag whose decode failed is not tried again for this long.
_DECODE_RETRY_S = 10.0

_RFID_DEBUG_RESPONSE = (
    "oams_rfid_debug_response gpiob_moder=%u gpiob_levels=%u "
    "gpiod_moder=%u gpiod_levels=%u spi0_config=%u spi0_last=%u "
    "spi1_config=%u spi1_last=%u")


def _bits_to_bytes(bits):
    """Pack bits (LSB first within each byte) into bytes."""
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index >> 3] |= 1 << (index & 7)
    return bytes(out)


def _bytes_to_bits(data):
    """Unpack bytes into bits, LSB first within each byte."""
    return [(byte >> k) & 1 for byte in data for k in range(8)]


class Mfrc522Error(Exception):
    pass


class Mfrc522NoCard(Mfrc522Error):
    pass


class Mfrc522:
    # Page 0: command and status registers
    COMMAND = 0x01
    COM_IRQ = 0x04
    ERROR = 0x06
    STATUS2 = 0x08
    FIFO_DATA = 0x09
    FIFO_LEVEL = 0x0A
    CONTROL = 0x0C
    BIT_FRAMING = 0x0D
    COLL = 0x0E

    # Page 1: configuration registers
    MODE = 0x11
    TX_CONTROL = 0x14
    TX_ASK = 0x15

    # Page 2: timer and CRC registers
    CRC_RESULT_H = 0x21
    CRC_RESULT_L = 0x22
    T_MODE = 0x2A
    T_PRESCALER = 0x2B
    T_RELOAD_H = 0x2C
    T_RELOAD_L = 0x2D

    VERSION = 0x37

    CMD_IDLE = 0x00
    CMD_CALC_CRC = 0x03
    CMD_TRANSCEIVE = 0x0C
    CMD_MF_AUTHENT = 0x0E
    CMD_SOFT_RESET = 0x0F

    PICC_REQA = 0x26
    PICC_WUPA = 0x52
    PICC_HLTA = 0x50
    PICC_ANTICOLL = (0x93, 0x95, 0x97)
    PICC_CASCADE_TAG = 0x88
    # ErrorReg CollErr; CollReg CollPosNotValid and CollPos.
    ERR_COLL = 0x08
    COLL_POS_NOT_VALID = 0x20
    #: At most this many tags are enumerated from one field (C2 §4.6.1).
    MAX_TAGS = 4
    PICC_MF_AUTH_KEY_A = 0x60
    PICC_MF_AUTH_KEY_B = 0x61
    PICC_MF_READ = 0x30

    # FM17580 VersionReg is a production-version byte with no fixed reset
    # value. OpenAMS hardware has been observed with both A1 and EE.
    VALID_VERSIONS = (0x88, 0x90, 0x91, 0x92, 0xA1, 0xB2, 0xEE)
    def __init__(self, spi, reactor=None):
        self.spi = spi
        self.reactor = reactor
        # The SAK of every tag the last read_uids() enumerated.
        self.saks = {}

    # These two methods are the transport-neutral contract used by the
    # OpenAMS RFID stack.
    def reg_read(self, reg):
        address = 0x80 | ((int(reg) << 1) & 0x7E)
        params = self.spi.spi_transfer([address, 0x00])
        response = params.get("response", b"")
        if len(response) != 2:
            raise Mfrc522Error("short SPI response while reading register 0x%02x" % reg)
        value = response[1]
        if not isinstance(value, int):
            value = ord(value)
        return value

    def reg_write(self, reg, value):
        address = (int(reg) << 1) & 0x7E
        self.spi.spi_send([address, int(value) & 0xFF])

    def reader_power(self, on):
        # OpenAMS has no separate coil-enable line. TxControlReg owns the RF
        # field; this method intentionally only satisfies the AFC link API.
        return None

    def _set_bits(self, reg, mask):
        self.reg_write(reg, self.reg_read(reg) | mask)

    def _clear_bits(self, reg, mask):
        self.reg_write(reg, self.reg_read(reg) & ~mask)

    def initialize(self):
        self.reg_write(self.COMMAND, self.CMD_SOFT_RESET)
        command_value = None
        for _ in range(50):
            command_value = self.reg_read(self.COMMAND)
            if not command_value & (1 << 4):
                break
            self._pause(0.001)
        else:
            raise Mfrc522Error(
                "soft reset timed out (CommandReg=0x%02x)" % command_value)

        # Proven FM17580/MFRC522 timer setup used by the AFC reader stack.
        self.reg_write(self.T_MODE, 0x8D)
        self.reg_write(self.T_PRESCALER, 0x3E)
        self.reg_write(self.T_RELOAD_H, 0x00)
        self.reg_write(self.T_RELOAD_L, 30)
        self.reg_write(self.TX_ASK, 0x40)
        self.reg_write(self.MODE, 0x3D)
        self.antenna_on()

        version = self.reg_read(self.VERSION)
        if version not in self.VALID_VERSIONS:
            raise Mfrc522Error("unexpected VersionReg value 0x%02x" % version)
        return version

    def antenna_on(self):
        if self.reg_read(self.TX_CONTROL) & 0x03 != 0x03:
            self._set_bits(self.TX_CONTROL, 0x03)

    def antenna_off(self):
        self._clear_bits(self.TX_CONTROL, 0x03)

    def _pause(self, seconds):
        if self.reactor is not None:
            self.reactor.pause(self.reactor.monotonic() + seconds)

    def _calculate_crc(self, data):
        # ISO14443-A CRC (poly 0x8408, init 0x6363). Doing this on the host is
        # dramatically cheaper than polling CalcCRC over CAN register reads.
        crc = 0x6363
        for value in data:
            value ^= crc & 0xFF
            value = (value ^ (value << 4)) & 0xFF
            crc = ((crc >> 8) ^ (value << 8) ^ (value << 3)
                   ^ (value >> 4)) & 0xFFFF
        return [crc & 0xFF, crc >> 8]

    def _communicate(self, command, send_data, tx_last_bits=0,
                     rx_align=0, check_crc=False, collision=False):
        """One command. With ``collision`` the result is ``(response,
        bit_length, coll_pos)``: ``coll_pos`` is None, or CollReg's position
        (1..32) of the first bit collision (several tags answered)."""
        wait_irq = 0x10 if command == self.CMD_MF_AUTHENT else 0x30
        self.reg_write(self.COMMAND, self.CMD_IDLE)
        self.reg_write(self.COM_IRQ, 0x7F)
        self._set_bits(self.FIFO_LEVEL, 0x80)
        for value in send_data:
            self.reg_write(self.FIFO_DATA, value)
        self.reg_write(self.BIT_FRAMING,
                       ((rx_align & 7) << 4) | (tx_last_bits & 7))
        self.reg_write(self.COMMAND, command)
        if command == self.CMD_TRANSCEIVE:
            self._set_bits(self.BIT_FRAMING, 0x80)

        for _ in range(100):
            irq = self.reg_read(self.COM_IRQ)
            if irq & wait_irq:
                break
            if irq & 0x01:
                raise Mfrc522NoCard("no card detected")
            self._pause(0.0005)
        else:
            raise Mfrc522Error("card communication did not complete")

        if command == self.CMD_TRANSCEIVE:
            self._clear_bits(self.BIT_FRAMING, 0x80)
        error = self.reg_read(self.ERROR)
        if error & 0x13:
            raise Mfrc522Error("MFRC522 ErrorReg=0x%02x" % error)

        count = self.reg_read(self.FIFO_LEVEL)
        if count > 64:
            raise Mfrc522Error("invalid FIFO length %d" % count)
        response = [self.reg_read(self.FIFO_DATA) for _ in range(count)]
        valid_bits = self.reg_read(self.CONTROL) & 0x07
        bit_length = (count - 1) * 8 + valid_bits if valid_bits else count * 8

        if check_crc:
            if len(response) < 2 or bit_length % 8:
                raise Mfrc522Error("response has no valid CRC")
            if self._calculate_crc(response[:-2]) != response[-2:]:
                raise Mfrc522Error("response CRC mismatch")
        if not collision:
            return response, bit_length
        coll_pos = None
        if error & self.ERR_COLL:
            coll = self.reg_read(self.COLL)
            if coll & self.COLL_POS_NOT_VALID:
                raise Mfrc522Error("collision outside the UID bits")
            coll_pos = (coll & 0x1F) or 32
        return response, bit_length, coll_pos

    def request(self, command=None):
        """REQA (or WUPA, which also wakes a halted tag): the ATQA."""
        command = self.PICC_REQA if command is None else command
        response, bits = self._communicate(
            self.CMD_TRANSCEIVE, [command], tx_last_bits=7)
        if len(response) != 2 or bits != 16:
            raise Mfrc522Error("invalid ATQA response")
        return response

    def halt(self):
        """HLTA: the selected tag stops answering REQA. A tag acknowledges
        nothing, so the command's timeout is its success."""
        frame = [self.PICC_HLTA, 0x00]
        frame += self._calculate_crc(frame)
        try:
            self._communicate(self.CMD_TRANSCEIVE, frame)
        except Mfrc522NoCard:
            return
        raise Mfrc522Error("tag answered HLTA")

    def _select(self, cascade_command, part):
        """SELECT one cascade level with its 4 UID bytes + BCC: the SAK."""
        select = [cascade_command, 0x70] + list(part)
        select += self._calculate_crc(select)
        sak, _ = self._communicate(
            self.CMD_TRANSCEIVE, select, check_crc=True)
        if len(sak) != 3:
            raise Mfrc522Error("invalid SAK response")
        return sak[0]

    def _anticollision(self, cascade_command):
        """ISO/IEC 14443-3 bit-wise anticollision for one cascade level.

        Walks the UID bit tree as ``klipper_rf``'s
        ``iso14443a._resolve_level`` does: each collision fixes the bits
        received before it, takes the 0-branch at the colliding bit, and asks
        again with the known bits, until exactly one tag answers all 40 bits
        (UID + BCC). Returns those 5 bytes. A wrong guess of where the
        collision is can only lose the tag (an error), never invent a UID:
        the BCC and the SELECT both check what was resolved."""
        known = []
        for _ in range(33):
            nbits = len(known)
            tx_last = nbits % 8
            frame = [cascade_command, ((2 + nbits // 8) << 4) | tx_last]
            frame += list(_bits_to_bytes(known))
            # ValuesAfterColl = 0: bits received after a collision read 0.
            self.reg_write(self.COLL, 0x00)
            try:
                response, bit_length, coll_pos = self._communicate(
                    self.CMD_TRANSCEIVE, frame, tx_last_bits=tx_last,
                    rx_align=tx_last, collision=True)
            except Mfrc522NoCard:
                if nbits:
                    raise Mfrc522Error("anticollision lost the tag")
                raise
            received = _bytes_to_bits(response)[tx_last:bit_length]
            if coll_pos is None:
                full = known + received
                if len(full) < 40:
                    raise Mfrc522Error("short anticollision response")
                part = _bits_to_bytes(full[:40])
                if part[0] ^ part[1] ^ part[2] ^ part[3] != part[4]:
                    raise Mfrc522Error("UID BCC mismatch")
                return list(part)
            index = self._collision_index(coll_pos, nbits, len(received))
            known = known + received[:index] + [0]
            if len(known) > 32:
                raise Mfrc522Error("anticollision overflow")
        raise Mfrc522Error("anticollision did not converge")

    @staticmethod
    def _collision_index(coll_pos, nbits, received):
        """Where in this answer's bits the collision is.

        CollPos counts from 1. How it counts once the request carried known
        bits is not settled for this chip (to be measured): read as the bit
        position in the whole UID (the common MFRC522 drivers), then as the
        position in this answer's FIFO bits, then as the position among the
        bits received. The first that falls inside the answer is used; a
        wrong one only makes the walk lose the tag (an error sample)."""
        for index in (coll_pos - 1 - nbits, coll_pos - 1 - nbits % 8,
                      coll_pos - 1):
            if 0 <= index < received:
                return index
        raise Mfrc522Error("collision position %d out of range" % coll_pos)

    def _select_cascade(self):
        """Anticollision and SELECT through every cascade level of one tag:
        ``(uid bytes, sak)``."""
        uid = []
        for cascade_command in self.PICC_ANTICOLL:
            part = self._anticollision(cascade_command)
            sak = self._select(cascade_command, part)
            if part[0] == self.PICC_CASCADE_TAG:
                uid.extend(part[1:4])
            else:
                uid.extend(part[:4])
            if not sak & 0x04:
                return bytes(uid), sak
        raise Mfrc522Error("UID cascade did not terminate")

    def read_uids(self, max_tags=None):
        """Every tag in the field (C2 §4.6.1): WUPA, then anticollision,
        SELECT and HLTA for one tag at a time, REQA again (a halted tag stays
        silent), until no tag answers or ``max_tags``. Returns the UIDs as
        bytes, ``[]`` for an empty field. Raises Mfrc522Error for a sample
        that went wrong: that is an error, never an empty field."""
        max_tags = self.MAX_TAGS if max_tags is None else max_tags
        uids = []
        self.saks = {}
        command = self.PICC_WUPA
        while len(uids) < max_tags:
            try:
                self.request(command)
            except Mfrc522NoCard:
                break
            command = self.PICC_REQA
            try:
                uid, sak = self._select_cascade()
            except Mfrc522NoCard:
                raise Mfrc522Error("tag lost during anticollision")
            if uid in uids:
                raise Mfrc522Error("tag %s answered twice" % uid.hex())
            uids.append(uid)
            self.saks[uid] = sak
            self.halt()
        return uids

    def select_uid(self, uid):
        """WUPA, then SELECT the tag with this UID at every cascade level
        (no anticollision: only that tag answers). Returns its SAK, ready
        for authentication and block reads; halt() it afterwards."""
        uid = bytes(uid)
        if len(uid) == 4:
            levels = [list(uid)]
        elif len(uid) == 7:
            levels = [[self.PICC_CASCADE_TAG] + list(uid[0:3]),
                      list(uid[3:7])]
        elif len(uid) == 10:
            levels = [[self.PICC_CASCADE_TAG] + list(uid[0:3]),
                      [self.PICC_CASCADE_TAG] + list(uid[3:6]),
                      list(uid[6:10])]
        else:
            raise ValueError("a UID has 4, 7 or 10 bytes")
        self.request(self.PICC_WUPA)
        sak = 0
        for cascade_command, part in zip(self.PICC_ANTICOLL, levels):
            bcc = part[0] ^ part[1] ^ part[2] ^ part[3]
            sak = self._select(cascade_command, part + [bcc])
        return sak

    def authenticate(self, block, key, uid, key_b=False):
        if len(key) != 6 or len(uid) < 4:
            raise ValueError("MIFARE authentication requires a 6-byte key and UID")
        auth = self.PICC_MF_AUTH_KEY_B if key_b else self.PICC_MF_AUTH_KEY_A
        frame = [auth, block] + list(key) + list(uid[-4:])
        self._communicate(self.CMD_MF_AUTHENT, frame)
        if not self.reg_read(self.STATUS2) & 0x08:
            raise Mfrc522Error("MIFARE authentication failed")

    def stop_crypto(self):
        self._clear_bits(self.STATUS2, 0x08)

    def read_block(self, block, key, uid, key_b=False):
        self.authenticate(block, key, uid, key_b)
        try:
            frame = [self.PICC_MF_READ, block]
            frame += self._calculate_crc(frame)
            data, bits = self._communicate(
                self.CMD_TRANSCEIVE, frame, check_crc=True)
            if len(data) != 18 or bits != 144:
                raise Mfrc522Error("invalid MIFARE block response")
            return bytes(data[:16])
        finally:
            self.stop_crypto()


class OpenAmsRfidRegistry:
    def __init__(self, printer):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.readers = {}
        self.drivers = {}
        printer.lookup_object("gcode").register_command(
            "OAMSM_RFID_READ", self.cmd_RFID_READ,
            desc="Read an OpenAMS RFID card reader")
        printer.register_event_handler("klippy:ready", self._handle_ready)

    def add_driver(self, driver, config):
        if driver.oams_index in self.drivers:
            raise config.error(
                "duplicate MFRC522 driver for OAMS=%d" % driver.oams_index)
        self.drivers[driver.oams_index] = driver
        for reader in driver.readers:
            key = (reader.oams_index, reader.rfid_card)
            if key in self.readers:
                raise config.error(
                    "duplicate MFRC522 mapping for OAMS=%d RFID_CARD=%d" % key)
            self.readers[key] = reader

    def _handle_ready(self):
        # SPI queries wait for MCU responses, so defer them out of Klippy's
        # pause-disabled ready handler. Each dual-reader driver owns one callback
        # and one poll timer for its shared bus.
        self.reactor.register_callback(self._handle_start)

    def _handle_start(self, eventtime):
        drivers = list(self.drivers.values())
        for driver in drivers:
            driver._initialize(eventtime)
        start_time = self.reactor.monotonic()
        for driver in drivers:
            self.reactor.register_timer(
                driver._poll, start_time + driver.poll_interval)

    def cmd_RFID_READ(self, gcmd):
        oams_index = gcmd.get_int("OAMS", None, minval=0)
        rfid_card = gcmd.get_int("RFID_CARD", None, minval=0, maxval=1)
        if oams_index is None or rfid_card is None:
            raise gcmd.error(
                "Usage: OAMSM_RFID_READ OAMS=<index> RFID_CARD=<0|1>")
        reader = self.readers.get((oams_index, rfid_card))
        if reader is None:
            raise gcmd.error(
                "No RFID reader configured for OAMS=%d RFID_CARD=%d"
                % (oams_index, rfid_card))
        reader.read_now()
        gcmd.respond_info(reader.format_last_read())


class OpenAmsMfrc522:
    """State and MFRC522 operations for one endpoint of a dual-reader driver."""

    def __init__(self, owner, name, rfid_card, spi, reset_pin):
        self.owner = owner
        self.printer = owner.printer
        self.reactor = owner.reactor
        self.name = name
        self.oams_index = owner.oams_index
        self.rfid_card = rfid_card
        self.spi = spi
        self.reset_pin = reset_pin
        self.reader = Mfrc522(self.spi, self.reactor)
        self.poll_interval = owner.poll_interval
        self.read_block_number = owner.read_block_number
        self.key = owner.key
        self.key_b = owner.key_b
        self.version = None
        # Every UID the last good sample enumerated (bytes), the first of
        # them as `uid`, and the decoded Bambu data of each (hex -> dict).
        self.uids = []
        self.uid = None
        self.records = {}
        self.block_data = None
        self.tag_data = None
        self.tag_processor = None
        self.last_error = None
        self.last_read_status = "NOT_READ"
        self.last_read_time = None
        self._decode_failed = {}
        self._firmware_debug_reported = False

        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "MFRC522_QUERY", "READER", self.name, self.cmd_QUERY,
            desc="Read an OpenAMS MFRC522 reader")

    @staticmethod
    def _parse_key(value):
        value = re.sub(r"[^0-9a-fA-F]", "", value)
        if len(value) != 12:
            raise Mfrc522Error("key must contain exactly six hexadecimal bytes")
        return bytes.fromhex(value)

    @staticmethod
    def _format_firmware_debug(params):
        def pin_state(port, pin, moder, levels):
            mode = (moder >> (pin * 2)) & 3
            idr = (levels >> pin) & 1
            odr = (levels >> (16 + pin)) & 1
            return "%s%d(mode=%d in=%d out=%d)" % (port, pin, mode, idr, odr)

        def spi_state(index):
            config = params.get("spi%d_config" % index, 0)
            last = params.get("spi%d_last" % index, 0)
            if not config:
                return "spi%d(unused)" % index
            encoded = (config >> 8) & 0xFF
            cs = ("NONE" if encoded == 0xFF else "P%s%d" % (
                chr(ord("A") + (encoded >> 4)), encoded & 0x0F))
            return (
                "spi%d(oid=%d cs=%s configured=%d active_high=%d mode=%d "
                "period_ticks=%d tx0=0x%02x rx0=0x%02x rx_last=0x%02x "
                "selected_cs=%d idle_cs=%d other_cs_idle=%d bus=%d count_mod16=%d)"
                % (index, config & 0xFF, cs, (config >> 16) & 1,
                   (config >> 18) & 1, (config >> 19) & 3, config >> 21,
                   last & 0xFF, (last >> 8) & 0xFF, (last >> 16) & 0xFF,
                   (last >> 24) & 1, (last >> 25) & 1,
                   (last >> 26) & 1, (last >> 31) & 1,
                   (last >> 27) & 0x0F))

        b_moder = params.get("gpiob_moder", 0)
        b_levels = params.get("gpiob_levels", 0)
        d_moder = params.get("gpiod_moder", 0)
        d_levels = params.get("gpiod_levels", 0)
        return " ".join((
            pin_state("PB", 0, b_moder, b_levels),
            pin_state("PB", 2, b_moder, b_levels),
            pin_state("PB", 3, b_moder, b_levels),
            pin_state("PD", 2, d_moder, d_levels),
            spi_state(0), spi_state(1)))

    def _log_firmware_debug(self):
        if self._firmware_debug_reported:
            return
        self._firmware_debug_reported = True
        try:
            command = self.spi.get_mcu().lookup_query_command(
                "oams_rfid_debug", _RFID_DEBUG_RESPONSE,
                cq=self.spi.get_command_queue())
            params = command.send([])
            logging.error("MFRC522 %s firmware RFID debug: %s",
                          self.name, self._format_firmware_debug(params))
        except Exception as exc:
            logging.info("MFRC522 %s firmware RFID diagnostics unavailable: %s",
                         self.name, exc)

    def _initialize(self, eventtime):
        self.tag_processor = self.owner.tag_processor
        try:
            self.version = self.owner.activate_reader(self, force=True)
            self.last_error = None
        except Exception as exc:
            self.last_read_status = "ERROR"
            self.last_read_time = eventtime
            self.last_error = str(exc)
            logging.exception("MFRC522 %s initialization failed", self.name)
            self._log_firmware_debug()

    def _decode(self, uid):
        """The decoded Bambu record of ``uid``, or None (not a Bambu tag,
        no processor, already known, a failed read).

        A tag is decoded only while no bay this reader covers is turning
        (a read that loses the tag half way is useless, C2 §4.6.1), once
        (the tag cache keeps it), and not again for 10 s after a failure.
        Block 9 (the spool's tray UUID) is read first: a tag whose sibling
        on the same spool was decoded before takes that record."""
        if self.tag_processor is None:
            return None
        tag_reader = self.owner.tag_reader()
        uid_hex = uid.hex()
        if tag_reader is not None:
            if not tag_reader.needs_record(uid_hex):
                return None
            if not tag_reader.reader_at_rest(self.rfid_card):
                return None
        failed = self._decode_failed.get(uid_hex)
        now = self.reactor.monotonic()
        if failed is not None and now - failed < _DECODE_RETRY_S:
            return None
        try:
            sak = self.reader.select_uid(uid)
            if sak not in _MIFARE_SAKS:
                return None
            data = None
            identity = getattr(self.tag_processor, "read_identity", None)
            if identity is not None and tag_reader is not None:
                key = tag_identity.spool_key(
                    {"format": "bambu", "uid": uid_hex,
                     "bambu": {"tray_uid": identity(self.reader, uid)}})
                known = tag_reader.record_for_key(key)
                if known is not None:
                    data = known["bambu"]
            if data is None:
                data = self.tag_processor.read_tag(self.reader, uid)
        except Exception as exc:
            self._decode_failed[uid_hex] = now
            if tag_reader is not None:
                tag_reader.note_decode_failed(uid_hex)
            logging.info("MFRC522 %s: tag %s not decoded: %s",
                         self.name, uid_hex, exc)
            return None
        finally:
            try:
                self.reader.halt()
            except Exception:
                pass
        self._decode_failed.pop(uid_hex, None)
        return {"format": "bambu", "bambu": data, "spoolman_id": None,
                "openspool": None}

    def _sample(self, eventtime):
        """One sample of this reader: every UID in its field, handed on as
        one ``mfrc522:field`` event. A sample that went wrong is an error
        (None), never an empty field (C2 §4.6.1).

        One sample at a time for the pair: a sample yields at every SPI
        round trip, and the poll timer, ``OAMS_RFID_SCAN BAY=`` and
        ``MFRC522_QUERY`` all sample; a second sample starting meanwhile
        would switch the antenna to the other reader under the first."""
        with self.owner._mutex:
            return self._sample_locked(eventtime)

    def _sample_locked(self, eventtime):
        self.last_read_time = eventtime
        try:
            self.version = self.owner.activate_reader(self)
            uids = self.reader.read_uids()
            records = {}
            tag_reader = self.owner.tag_reader()
            for uid in uids:
                record = self._decode(uid)
                if (record is None and self.tag_processor is not None
                        and tag_reader is not None
                        and self.reader.saks.get(uid) in _MIFARE_SAKS
                        and tag_reader.needs_record(uid.hex())):
                    # Not decoded yet (seen while passing): a Bambu tag
                    # whose content is pending (C2 §4.6).
                    record = tag_reader.pending_record(uid.hex())
                if record is not None:
                    records[uid.hex()] = record
            block = None
            if self.read_block_number is not None and uids:
                self.reader.select_uid(uids[0])
                try:
                    block = self.reader.read_block(
                        self.read_block_number, self.key, uids[0], self.key_b)
                finally:
                    self.reader.halt()
        except Mfrc522Error as exc:
            self.last_read_status = "ERROR"
            self.last_error = str(exc)
            self.printer.send_event("mfrc522:field", self, None, {})
            return None
        except Exception as exc:
            self.last_read_status = "ERROR"
            self.last_error = str(exc)
            logging.exception("MFRC522 %s polling failed", self.name)
            self.printer.send_event("mfrc522:field", self, None, {})
            return None
        if [u.hex() for u in uids] != [u.hex() for u in self.uids]:
            logging.info("MFRC522 %s field: %s", self.name,
                         ",".join(u.hex() for u in uids) or "empty")
        self.uids = list(uids)
        self.uid = uids[0] if uids else None
        self.records.update(records)
        self.records = dict((u.hex(), self.records[u.hex()]) for u in uids
                            if u.hex() in self.records)
        self.block_data = block
        self.tag_data = (None if self.uid is None
                         else self.records.get(self.uid.hex(), {}).get(
                             "bambu"))
        self.last_read_status = "PRESENT" if uids else "EMPTY"
        self.last_error = None
        hexes = [u.hex() for u in uids]
        self.printer.send_event("mfrc522:field", self, hexes, records)
        return hexes

    def read_now(self):
        return self._sample(self.reactor.monotonic())

    def get_status(self, eventtime):
        return {
            "oams": self.oams_index,
            "rfid_card": self.rfid_card,
            "present": self.uid is not None,
            "uid": None if self.uid is None else self.uid.hex(),
            "uids": [uid.hex() for uid in self.uids],
            "block": None if self.block_data is None else self.block_data.hex(),
            "filament": self.tag_data,
            "version": self.version,
            "last_read_status": self.last_read_status,
            "last_read_time": self.last_read_time,
            "error": self.last_error,
        }

    def format_last_read(self):
        current_status = "PRESENT" if self.uid is not None else "EMPTY"
        uid = "NONE" if self.uid is None else self.uid.hex().upper()
        block = ("NONE" if self.block_data is None
                 else self.block_data.hex().upper())
        version = ("UNKNOWN" if self.version is None
                   else "0x%02X" % self.version)
        filament = ("NONE" if self.tag_data is None else
                    json.dumps(self.tag_data, sort_keys=True, separators=(",", ":")))
        error = "NONE" if self.last_error is None else self.last_error
        uids = ",".join(u.hex().upper() for u in self.uids) or "NONE"
        return (
            "OAMSM_RFID_READ OAMS=%d RFID_CARD=%d STATUS=%s "
            "LAST_READ_STATUS=%s UID=%s BLOCK=%s VERSION=%s FILAMENT=%s ERROR=%s"
            " UIDS=%s"
            % (self.oams_index, self.rfid_card, current_status,
               self.last_read_status, uid, block, version, filament, error,
               uids))

    def cmd_QUERY(self, gcmd):
        self.read_now()
        gcmd.respond_info(self.format_last_read())


class OpenAmsMfrc522Pair:
    """One Klipper object that owns and serializes both OpenAMS readers."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.oams_index = config.getint("oams", minval=0)
        self.poll_interval = config.getfloat(
            "poll_interval", 0.5, minval=0.1)
        self.read_block_number = config.getint(
            "read_block", None, minval=0, maxval=255)
        self.key = OpenAmsMfrc522._parse_key(
            config.get("key", "FFFFFFFFFFFF"))
        self.key_b = config.getboolean("key_b", False)
        # While a bay one reader covers turns or was just inserted, only
        # that reader is sampled, this often (C2 §4.6.1; the floor is the
        # fastest a UID enumeration allows, still to be measured).
        self.focus_poll_interval = config.getfloat(
            "focus_poll_interval", 0.1, minval=0.1)
        self.tag_processor = None
        self.active_reader = None
        # Serialises the two readers' SPI work (OpenAmsMfrc522._sample).
        self._mutex = self.reactor.mutex()
        # C2 §4.6: which bays each reader's field covers. The defaults split
        # the four bays in two (still to be confirmed on the hardware); a
        # mainboard whose readers cover other bays sets them.
        self.bays = {
            0: self._parse_bay_list(config.get("bays_a", "0,1")),
            1: self._parse_bay_list(config.get("bays_b", "2,3")),
        }
        # The attribution thresholds still to be measured on the hardware,
        # with their starting values (src/oams_tags.py).
        self.attribution_options = {
            "debounce": config.getint(
                "field_debounce", oams_tags.FIELD_DEBOUNCE, minval=1,
                maxval=10),
            "hand_s": config.getfloat(
                "handling_window", oams_tags.T_HAND_S, minval=0.),
            "orphan_s": config.getfloat(
                "orphan_window", oams_tags.ORPHAN_S, minval=0.),
            "static_samples": config.getint(
                "static_samples", oams_tags.N_STATIC, minval=2),
            "no_tag_turn_s": config.getfloat(
                "no_tag_turn_time", oams_tags.NO_TAG_TURN_S, minval=1.),
            "static_turn_s": config.getfloat(
                "static_turn_time", oams_tags.STATIC_TURN_S, minval=0.),
            "static_turn_mm": config.getfloat(
                "static_turn_length", oams_tags.STATIC_TURN_MM, minval=0.),
        }
        self._attached = None

        spi_a = bus.MCU_SPI_from_config(
            config, mode=0, pin_option="cs_pin_a", default_speed=100000)
        spi_b = bus.MCU_SPI_from_config(
            config, mode=0, pin_option="cs_pin_b", default_speed=100000)
        if spi_a.get_mcu() is not spi_b.get_mcu():
            raise config.error("both OpenAMS RFID readers must use one MCU")

        ppins = self.printer.lookup_object("pins")
        reset_a = ppins.setup_pin(
            "digital_out", config.get("reset_pin_a"))
        reset_b = ppins.setup_pin(
            "digital_out", config.get("reset_pin_b"))
        for reset_pin in (reset_a, reset_b):
            reset_pin.setup_start_value(1, 1)

        self.readers = (
            OpenAmsMfrc522(
                self, "rfid_a", 0, spi_a, reset_a),
            OpenAmsMfrc522(
                self, "rfid_b", 1, spi_b, reset_b),
        )

        registry = self.printer.lookup_object("oamsm_rfid_registry", None)
        if registry is None:
            registry = OpenAmsRfidRegistry(self.printer)
            self.printer.add_object("oamsm_rfid_registry", registry)
        registry.add_driver(self, config)

        # Every sample goes to the OAMS unit's TagReader (src/oams_tags.py),
        # which attributes tags to bays (C2 §4.6). Every pair registers;
        # each ignores an event from a reader it does not own.
        self.printer.register_event_handler("mfrc522:field",
                                            self._handle_field)

    @staticmethod
    def _parse_bay_list(value):
        return tuple(int(item) for item in str(value).split(",")
                    if item.strip())

    def _find_oams(self):
        lookup = getattr(self.printer, "lookup_objects", None)
        if lookup is None:
            return None
        for _, obj in lookup(module="oams"):
            if getattr(obj, "oams_idx", None) == self.oams_index:
                return obj
        return None

    def tag_reader(self):
        """The TagReader of this pair's unit, attached to this pair."""
        oams = self._find_oams()
        tag_reader = getattr(oams, "tag_reader", None)
        if tag_reader is None:
            return None
        if self._attached is not tag_reader:
            tag_reader.attach_readers(
                self, [list(self.bays[0]), list(self.bays[1])],
                **self.attribution_options)
            self._attached = tag_reader
        return tag_reader

    def _handle_field(self, reader, uids, records):
        """One reader's sample (UIDs, or None for an error) to the unit's
        tag attribution. Which bay a tag belongs to is decided there, from
        spool motion; this file attributes nothing."""
        if reader not in self.readers:
            return
        tag_reader = self.tag_reader()
        if tag_reader is not None:
            tag_reader.note_field(reader.rfid_card, uids,
                                  self.reactor.monotonic(), records=records)

    def sample_reader(self, index):
        """Sample one reader now (``OAMS_RFID_SCAN BAY=``): its UIDs, or
        None when the sample went wrong. An explicit read decodes a tag at
        rest now, whenever its last decode failed."""
        reader = self.readers[index]
        reader._decode_failed.clear()
        return reader._sample(self.reactor.monotonic())

    def activate_reader(self, reader, force=False):
        """Select one reader and ensure only its RF field is enabled."""
        reader_index = reader.rfid_card
        if (not force and self.active_reader == reader_index
                and reader.version is not None):
            return reader.version

        if self.active_reader is not None:
            previous = self.readers[self.active_reader]
            if previous.version is not None:
                previous.reader.antenna_off()

        if force or reader.version is None:
            reader.version = reader.reader.initialize()
        else:
            reader.reader.antenna_on()
        self.active_reader = reader_index
        return reader.version

    def _initialize(self, eventtime):
        self.tag_processor = self.printer.lookup_object(
            "bambu_lab_tag_processor", None)
        self.tag_reader()
        with self._mutex:
            for reader in self.readers:
                reader._initialize(eventtime)

    def _poll(self, eventtime):
        """Both readers in turn every ``poll_interval``; only the readers
        covering a turning or just-inserted bay, every
        ``focus_poll_interval``, while there are any (C2 §4.6.1)."""
        tag_reader = self.tag_reader()
        focus = []
        if tag_reader is not None:
            focus = [index for index in tag_reader.focus_readers(eventtime)
                     if 0 <= index < len(self.readers)]
        for reader in ([self.readers[i] for i in focus] or self.readers):
            reader._sample(eventtime)
        interval = self.focus_poll_interval if focus else self.poll_interval
        wake = eventtime + interval
        now = self.reactor.monotonic()
        if wake <= now:
            wake = now + interval    # a slow sample never runs back to back
        return wake

    def get_status(self, eventtime):
        return {
            "oams": self.oams_index,
            "readers": [reader.get_status(eventtime) for reader in self.readers],
        }


def load_config(config):
    return OpenAmsMfrc522Pair(config)


def load_config_prefix(config):
    return OpenAmsMfrc522Pair(config)
