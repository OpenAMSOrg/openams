# Filament Buffer Pressure Sensor (FPS) — one feed-path lane
#
# Copyright (C) 2023-2026 JR Lomas (discord:knight_rad.iant) <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# An FPS is the pressure sensor + buffer that joins a lane's OAMS units to one
# toolhead extruder. The pressure value is produced by the FPS hardware and read
# here via the FPS MCU's ADC (host-side view; the OpenAMS 1 mainboard also reads
# the same analog signal locally to run its hub-motor control). Multiple FPS may be
# defined (one per toolhead) using named sections [fps <name>]; a single unnamed
# [fps] also works for the common one-lane case.


class FPS:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()              # "fps" or "fps <name>"
        self.fps_name = self.name.split()[-1]      # short lane key

        self.fps_value = 0.0
        self.source_oams_name = config.get('source_oams', None)
        self.source_oams = None
        self._source_is_mmu = False

        self._pin = config.get('pin', None)
        if (self._pin is None) == (self.source_oams_name is None):
            raise config.error(
                "[%s]: specify exactly one of 'pin' or 'source_oams'"
                % (self.name,))
        self._sample_count = config.getint('sample_count', 5)
        self._sample_time = config.getfloat('sample_time', 0.005)
        self._report_time = config.getfloat('report_time', 0.100)
        self._reversed = config.getboolean('reversed', False)

        # Host-confirmed toolhead arrival (C1 AMS_CAP_HOST_ARRIVAL_CONFIRM): a
        # load on a unit that cannot sense arrival itself is confirmed when
        # this lane's arrival reading rises to arrival_threshold and holds
        # there for arrival_debounce seconds. A lane with a `pin` has
        # such a signal; it must come from the toolhead/FPS side, never from
        # the unit's own buffer telemetry. A `source_oams` lane qualifies only
        # when that unit reports AMS_CAP_FPS_TOOLHEAD=1, meaning its relayed
        # fps_value is the position of a toolhead-side FPS/buffer.
        self.arrival_threshold = config.getfloat('arrival_threshold', 0.65)
        self.arrival_debounce = config.getfloat('arrival_debounce', 0.3)
        if not 0.0 <= self.arrival_threshold <= 1.0:
            raise config.error("[%s]: arrival_threshold must be between 0 and"
                               " 1" % (self.name,))
        if self.arrival_debounce < 0.0:
            raise config.error("[%s]: arrival_debounce must not be negative"
                               % (self.name,))

        # Reserved tuning parameters (kept for config compatibility / future use).
        self._sf_max_speed = config.getfloat('max_speed', 300.0)
        self._accel = config.getfloat('accel', 0.0)
        self._set_point = config.getfloat('set_point', 0.5)
        # Accepted for config compatibility; the ADC API is now detected from
        # the running Klipper/Kalico instead of trusting this flag.
        config.getboolean('use_kalico', False)

        # The toolhead extruder this lane feeds (resolved at connect).
        self.extruder_name = config.get('extruder', 'extruder')
        self.extruder = None

        self.adc = None
        if self._pin is not None:
            self.ppins = self.printer.lookup_object('pins')
            self.adc = self.ppins.setup_pin('adc', self._pin)
            if hasattr(self.adc, 'setup_adc_sample'):
                # Current mainline Klipper API:
                #   setup_adc_sample(report_time, sample_time, sample_count)
                #   setup_adc_callback(callback)  # list of samples
                self.adc.setup_adc_sample(
                    self._report_time, sample_time=self._sample_time,
                    sample_count=self._sample_count)
                self.adc.setup_adc_callback(self._adc_callback)
            else:
                # Kalico / pre-2024 mainline MCU_adc API.
                self.adc.setup_minmax(self._sample_time, self._sample_count)
                self.adc.setup_adc_callback(
                    self._report_time, self._adc_callback_scalar)

        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)

    def _handle_connect(self):
        self.extruder = self.printer.lookup_object(self.extruder_name, None)
        if self.extruder is None:
            raise self.printer.config_error(
                "[%s]: extruder '%s' not found; check the 'extruder:' option"
                % (self.name, self.extruder_name))
        if self.source_oams_name is not None:
            object_name = self.source_oams_name
            if not object_name.startswith(('oams ', 'klipper_mmu ')):
                object_name = 'oams ' + object_name
            source = self.printer.lookup_object(object_name, None)
            if source is None:
                raise self.printer.config_error(
                    "[%s]: source_oams '%s' was not found"
                    % (self.name, self.source_oams_name))
            if object_name.startswith('klipper_mmu '):
                self._source_is_mmu = True
                get_unit = getattr(source, 'get_openams_unit', None)
                source = get_unit() if callable(get_unit) else None
                if source is None:
                    raise self.printer.config_error(
                        "[%s]: source_oams '%s' has no OpenAMS unit"
                        % (self.name, self.source_oams_name))
            self.source_oams = source

    def _adc_callback(self, samples):
        # Mainline Klipper delivers a list of (read_time, value); use the latest.
        read_time, read_value = samples[-1]
        self._update_value(read_value)

    def _adc_callback_scalar(self, read_time, read_value):
        # Kalico delivers one (read_time, value) pair per report.
        self._update_value(read_value)

    def _update_value(self, read_value):
        if self._reversed:
            read_value = 1.0 - read_value
        self.fps_value = read_value

    def get_status(self, eventtime):
        try:
            value = self.get_value()
        except ValueError:
            if not self._source_is_mmu:
                raise
            value = None
        return {'fps_value': value}

    def get_value(self):
        if self.source_oams is not None:
            value = self.source_oams.fps_value
            return 1.0 - value if self._reversed else value
        return self.fps_value


def load_config(config):
    # Unnamed [fps] (single-lane setups).
    return FPS(config)


def load_config_prefix(config):
    # Named [fps <name>] (multi-lane / IDEX / multi-tool setups).
    return FPS(config)
