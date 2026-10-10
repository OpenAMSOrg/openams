# Switch input qualification

`km_switch_gpio_sample` normalizes each GPIO level by its configured polarity,
then requires 5,000 microseconds of unchanged observations before accepting a
transition. A sample more than 2,500 microseconds after the preceding sample
ends the previous evidence window and starts qualification again. These fixed
bounds match the controller's nominal 1 ms sampling period while tolerating
bounded callback/read time.

The frontend keeps the last accepted level separate from the current raw
sample, so a brief reversal cannot publish an unqualified transition. While a
transition is being qualified it continues publishing the last accepted state
as valid. Alternating raw bounce can retain that level indefinitely when samples
remain timely; the acquisition timestamp describes the latest sample, not
proof that the raw signal equals the accepted level. Initial
setup and a long sampling gap have no accepted state and require a complete
stable window before data becomes valid. Bay,
physical hub, and toolhead readings share a coherent validity batch; load
inputs retain per-lane validity. The switched-buffer service likewise retains
its last accepted state during qualification and invalidates on startup or a
sampling gap.

A valid batch carries the acquisition time of its latest read as the sensor
freshness timestamp. The debounce candidate start time is separate and is used
only to decide whether to accept a transition; it is never substituted for
freshness.

There is no switch raw-level query API, so this change does not add a separate
raw diagnostic surface. The debounce interval and maximum sample gap are
compile-time constants, not per-profile settings. Analog DiffA/DiffB sampling is
unchanged.
