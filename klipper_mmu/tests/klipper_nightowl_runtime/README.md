# NightOwl STM32 hosted startup candidates

The pinned release matrix saves safe and actuator-permitted USB images on both
H723, F405 and F407 for the same four-lane NightOwl profile. They use the shared
startup, local GPIO, and TMC UART path. The Custom pin maps, active levels,
pulls, UART addresses, and current
values in the matrix are explicit examples for compile and hosted tests; they
are not verified NightOwl or controller wiring.

The test runner checks the saved image manifest, Kconfig values, and Klipper
command dictionary, then compiles the production startup and controller
sources against that image configuration. The safe image reaches diagnostic
READY but rejects START with every feeder idle. The actuation image uses
synthetic inputs to test physical HUB and per-lane LOAD/BAY transitions through
production LOAD, FOLLOW, and UNLOAD commands, terminal retention/ACK, lane
isolation, and safe-off. After that cycle it starts one more lane-0 LOAD with
bay and load present, and hub and toolhead inactive, proves steps while the host
lease is held, then withholds heartbeats until the configured lease expires and
expects KM_FAULT_HOST_COMM_LOST, fault/stopped, all feeders off, retained
KM_EVENT_LOAD_FAILED, and no other-lane steps.

This is hosted software evidence only. It does not establish STM32 pin routing,
electrical levels, TMC UART response, motor current, step timing, or mechanical
NightOwl operation. Do not treat the example pins or actuation image as
qualified hardware settings.
