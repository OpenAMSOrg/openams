# Startup composition native test

`make test` builds the real diagnostic startup, board startup, controller,
commands, messages, and portable core together. It reuses the existing board
startup fixture's saved configuration and peripheral fakes to avoid a second
large shim implementation; its controller stand-ins are renamed so the linked
production controller is the only one called by startup. The local shim models
timers, GPIO ownership, clock, MCU identity, and calibration. Each case runs in
a separate process because startup and shutdown latches persist until reset.

Cases cover diagnostic command through the board's `NULL, 0` begin, pending
TMC confirmation and controller/command attachment to READY; missing calibration
before physical setup; TMC activation fault and timeout after setup. They also
check transport visibility and that START remains disabled without the actuation
build option. This is native simulation; no hardware is accessed.

Powered-rewind cases link the production PWM binding and portable bridge driver
to a fake local GPIO and paired-PWM surface. They check that both rewind lanes
stay disabled while TMC confirmation is pending, reach READY with the controller,
and turn off during shutdown. A modeled whole-timer route conflict fails before
peripheral setup. A busy second pair cleans up the first lane. An update failure
turns off the affected bridge immediately and shutdown turns off the other lane.
The pair surface models API acknowledgements and safe-off; it does not qualify
timer timing or physical output levels.

Run artifacts are written under `klipper-mmu/build/klipper_startup_composition/`.
