# Hosted startup for switched-buffer BoxTurtle

Selecting the ordinary STM32H723 BoxTurtle/AFC Lite switched-buffer preset now
defaults to the shared hosted diagnostic startup. The default configuration
uses the existing H723 per-reset identity provider and registers the normal
Klipper startup command only after the host has completed MCU configuration.
The legacy early `boxturtle_boot.c` task remains available when hosted startup
is explicitly disabled; the default Kconfig route omits it.

The switched preset retains its own pin map: HUB PC4, BAY0 PC5, buffer inputs
PE12/PE13, and LOAD PE8-PE11. DiffA/DiffB remains a separate option using
analog PC4/PC5 with HUB PE12 and BAY0 PE13. The release matrix saves exact
safe and actuator-permitted switched images, including `.config`,
`autoconf.h`, dictionary, ELF, and binary. Actuator permission remains off by
default; the enabled image is a software-only candidate, not physical
qualification.

The pinned release matrix checks the exact switched dictionaries with
Klipper's host wire wrappers. Its switched host-join fixture runs the
production H723 startup, GPIO sensor sampler, controller, operation handlers,
event retention, and exact ACK through a native process for all four lanes.
The sensor edges, GPIO reads, clocks, and driver effects are simulated; no
hardware or serial device is opened. The optional OpenAMS command-route case
remains skipped unless `OPENAMS_SOURCE` is set.

Run the matrix from the package root with:

```sh
python3 -B tests/release_matrix/run.py --build-dir /absolute/path/to/new-results
```

The focused switched host join can also be run against a saved image and its
matching dictionary:

```sh
make -s -C tests/host_h723_command_join MODE=switched \
  IMAGE_AUTOCONF=/absolute/path/to/autoconf.h \
  KLIPPER_DICTIONARY=/absolute/path/to/klipper.dict test
```

Root review runs both feedback modes with the saved `autoconf.h` unchanged:
`build/review-p9-switched-final.log` and `build/review-p9-diff-final.log` each
pass four hosted cases, including both OpenAMS command-route cases with the
source dependency provided. The native fixtures simulate electrical inputs and
driver effects. The default switched image's hosted startup and permission gates
are also asserted by the release matrix; combined clean-release acceptance is
tracked in `IMPLEMENTATION_STATUS.md`.
