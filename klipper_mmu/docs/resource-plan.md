<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Hardware resource-plan validator

The resource-plan validator performs setup-time verification of a complete,
canonical hardware allocation plan for the optional Klipper-MMU add-on. It is a
pure, allocation-free, bounded C11 function:

```c
KM_API km_result_t km_resource_plan_validate(const km_resource_claim_t *claims,
                                             size_t count,
                                             km_resource_diagnostic_t *diagnostic);
```

The validator operates strictly within one MCU hardware namespace. It checks
mutual exclusion, timer sharing rules, ADC serialization constraints, and
required companion dependencies before physical GPIO, timer, or ADC peripherals
are configured.

## What this validator is and is NOT

> [!IMPORTANT]
> The validator is **not** a runtime ownership registry, dynamic hardware arbiter,
> or reservation engine. Passing validation does not claim, configure, or reserve
> physical hardware on the MCU.

Plain boundaries of this module:
- **No hardware access:** The validator never touches MCU registers, reads GPIO states,
  arms interrupts, or enables outputs.
- **No dynamic allocation:** It executes in zero heap allocations, using only bounded
  caller-provided stack/memory structures.
- **No string parsing or alias resolution:** Board and profile code must resolve
  connector aliases (e.g. `HUB`, `TRG1`, `SW1`, `SW2`) into canonical numeric physical IDs
  before invoking validation.
- **No electrical or capability qualification:** Matching abstract numeric constraints
  does not verify that a physical pin supports the requested alternate function,
  voltage domain, slew rate, or drive strength.
- **No protection against omission or bypass:** The validator cannot detect missing
  peripheral claims, unmodeled external Klipper code using hardware out-of-band, or
  concurrent plan corruption.
- **Not a replacement for runtime arbitration:** Static resource allocation is distinct
  from runtime shared-filament-path arbitration in `operation_core`.

## Public data contract

All claim identities are purely numeric and canonical. No string pointers, dynamic
handles, or borrowed structures are used:

```c
typedef struct {
    km_resource_kind_t kind;
    uint16_t instance;
    uint16_t subresource;
    uint16_t owner;
    uint32_t period_ticks;
    uint32_t tick_hz;
} km_resource_claim_t;
```

### Capacity and complexity bounds
- `KM_RESOURCE_MAX_CLAIMS = 128u`: Bounded plan capacity. Passing `count > 128` is
  rejected immediately as `KM_ERR_INVALID_PARAM` with `KM_RESOURCE_DIAG_BAD_ARGUMENT`
  before dereferencing `claims`.
- Execution complexity is bounded $O(\text{count}^2)$. This is appropriate for
  board setup and configuration checks; it must never be invoked in real-time
  interrupt handlers or high-frequency control loops.

### Resource kinds
Six explicit kinds are defined with fixed non-zero integers:

| Kind enum | Value | Description |
| --- | --- | --- |
| `KM_RESOURCE_PIN` | 1 | Physical GPIO pin instance. Exclusive to one function. |
| `KM_RESOURCE_TIMER_EXCLUSIVE` | 2 | Dedicated timer instance reserved for a single non-PWM function. |
| `KM_RESOURCE_TIMER_PWM_BASE` | 3 | Base timer counter configuration for edge-aligned up-counting PWM. |
| `KM_RESOURCE_TIMER_CHANNEL` | 4 | Individual PWM channel on a configured timer base. |
| `KM_RESOURCE_ADC_UNIT` | 5 | Acquisition engine unit reservation for serialized multi-channel sampling. |
| `KM_RESOURCE_ADC_CHANNEL` | 6 | Analog conversion channel on a reserved ADC unit. |

Any other kind value (including 0, negative casts, or integers $\ge 7$) is rejected
as `KM_RESOURCE_DIAG_BAD_CLAIM`.

### Field constraints

| Field | Range | Constraints and semantics |
| --- | --- | --- |
| `instance` | `0 .. 65534` | Canonical physical pin ID, timer unit ID, or ADC unit ID. Instance 0 is valid (e.g. PA0). `UINT16_MAX` (`65535`) is reserved and invalid. |
| `owner` | `1 .. 65534` | Identifier of the configuring subsystem (e.g. feeder driver, rewinder bridge, analog engine). Owner 0 and `UINT16_MAX` are invalid. |
| `subresource` | Kind-dependent | `0` for `PIN`, `TIMER_EXCLUSIVE`, `TIMER_PWM_BASE`, `ADC_UNIT`. `1 .. 65534` for `TIMER_CHANNEL` (subresource 0 is invalid). `0 .. 65534` for `ADC_CHANNEL` (channel 0 is valid). |
| `period_ticks` | Kind-dependent | Strict $> 0$ for `TIMER_PWM_BASE`. Must be exactly $0$ for all other kinds. |
| `tick_hz` | Kind-dependent | Strict $> 0$ for `TIMER_PWM_BASE`. Must be exactly $0$ for all other kinds. |

## Concrete sharing and exclusion semantics

### 1. Pin exclusivity
A physical pin (`KM_RESOURCE_PIN`) is strictly exclusive by its canonical `instance`.
Two pin claims with the same `instance` conflict (`KM_RESOURCE_DIAG_DUPLICATE_RESOURCE`)
regardless of whether the owner is the same or different. Duplicate pin claims by the
same owner represent a configuration conflict, not harmless reuse.

### 2. Timer exclusive claims
`KM_RESOURCE_TIMER_EXCLUSIVE` reserves an entire hardware timer peripheral. It conflicts
with any other `TIMER_EXCLUSIVE`, `TIMER_PWM_BASE`, or `TIMER_CHANNEL` on the same timer
`instance`. An exclusive timer claim stands alone and needs no companion base.

### 3. PWM base sharing
`KM_RESOURCE_TIMER_PWM_BASE` represents the v1 timer sharing contract: ordinary
edge-aligned up-counting PWM with identical counter clock rate and period.
- Two base claims on the same timer instance from **different owners** are compatible
  if and only if both `period_ticks` and `tick_hz` are identical.
- If timing differs (`period_ticks` or `tick_hz` mismatch), the conflict reason is
  `KM_RESOURCE_DIAG_TIMER_CONFIGURATION`.
- Two base claims on the same timer instance with the **same owner** and matching
  timing are rejected as `KM_RESOURCE_DIAG_DUPLICATE_RESOURCE`. Same-owner mismatched
  timing is rejected as `KM_RESOURCE_DIAG_TIMER_CONFIGURATION`.
- An orphan base without channels is permitted as a reservation.

### 4. Timer channels and companion requirements
- `KM_RESOURCE_TIMER_CHANNEL` is exclusive by `(instance, subresource)`. Two channel
  claims on the same timer and same channel number conflict regardless of owner.
- Every timer channel requires **exactly one** `KM_RESOURCE_TIMER_PWM_BASE` with the
  matching `instance` and matching `owner` somewhere in the complete plan.
- Channel-to-base claim ordering does not matter; channels may appear before or after
  their base.
- If a timer channel has no matching base owned by the same component, validation
  fails with `KM_ERR_INVALID_PARAM` and `KM_RESOURCE_DIAG_MISSING_TIMER_BASE`.

### 5. ADC unit serialization and channel companions
- `KM_RESOURCE_ADC_UNIT` reserves an analog acquisition engine for a single serialized
  owner (such as the paired analog buffer sensor).
- ADC channels on the same unit **cannot have different owners**. Any two ADC claims
  (unit or channel) on the same ADC instance with different owners conflict with
  `KM_RESOURCE_DIAG_DUPLICATE_RESOURCE`.
- Two `ADC_UNIT` claims on the same instance conflict even with the same owner.
- Two `ADC_CHANNEL` claims on the same instance and subresource conflict even with
  the same owner.
- An `ADC_UNIT` and `ADC_CHANNEL` with matching instance and owner are compatible.
  Different channels on the same unit with the same owner are compatible.
- Every `ADC_CHANNEL` requires **exactly one** `ADC_UNIT` with matching `instance`
  and matching `owner`. An orphan `ADC_UNIT` without channels is permitted as a
  reservation.

### 6. Independent namespaces and board mapper duties
Pins, timers and ADC units have separate numeric ID namespaces. All timer kinds
share the timer namespace, and both ADC kinds share the ADC unit namespace:
- Pin ID 2 does not collide with Timer 2 or ADC unit 2.
- GPIO claims and peripheral claims are verified independently. The board mapper
  must emit both the peripheral claims (`TIMER_CHANNEL`, `ADC_CHANNEL`) and the
  corresponding physical `PIN` claims for routed MCU pins. The validator cannot
  infer hardware alternate function routing (e.g. `PA0 -> TIM2_CH1` or `PC4 -> ADC1_IN4`)
  from numeric IDs alone.

## Diagnostic precedence and ordering

The caller may optionally pass a pointer to a `km_resource_diagnostic_t` structure.
When provided, this structure is deterministically populated on every return.

> [!NOTE]
> The diagnostic structure must be independent caller storage and must not alias into
> the claims array.

Diagnostics follow strict five-phase precedence:

1. **Plan-level arguments:**
   If `count > KM_RESOURCE_MAX_CLAIMS` or (`count > 0` and `claims == NULL`):
   Returns `KM_ERR_INVALID_PARAM`, reason `KM_RESOURCE_DIAG_BAD_ARGUMENT`,
   `first_index = KM_RESOURCE_NO_INDEX`, `second_index = KM_RESOURCE_NO_INDEX`.
   *(An empty plan with `count == 0` is valid and returns `KM_OK` with `NONE`).*
2. **Individual record validation:**
   Records are scanned in ascending index order $i \in [0, \text{count}-1]$. The first
   record with an invalid kind, owner, instance, subresource, or timing field immediately
   returns `KM_ERR_INVALID_PARAM`, reason `KM_RESOURCE_DIAG_BAD_CLAIM`,
   `first_index = i`, `second_index = KM_RESOURCE_NO_INDEX`.
3. **Lexicographical pair conflict scan:**
   All pairs $(i, j)$ with $0 \le i < j < \text{count}$ are evaluated in lexicographical
   order. The first incompatible pair immediately returns `KM_ERR_CONFLICT`,
   `first_index = i`, `second_index = j`.
   The reason is `KM_RESOURCE_DIAG_TIMER_CONFIGURATION` for mismatched PWM base timing,
   or `KM_RESOURCE_DIAG_DUPLICATE_RESOURCE` for all other mutual exclusion violations.
4. **Channel companion dependency scan:**
   Channels are scanned in ascending index order $i \in [0, \text{count}-1]$ for missing
   prerequisite bases or units. The first missing companion returns `KM_ERR_INVALID_PARAM`,
   `first_index = i`, `second_index = KM_RESOURCE_NO_INDEX`.
   The reason is `KM_RESOURCE_DIAG_MISSING_TIMER_BASE` or `KM_RESOURCE_DIAG_MISSING_ADC_UNIT`.
5. **Success:**
   Returns `KM_OK`, reason `KM_RESOURCE_DIAG_NONE`,
   `first_index = KM_RESOURCE_NO_INDEX`, `second_index = KM_RESOURCE_NO_INDEX`.

Rejected plans leave all caller claim bytes completely untouched.

## Lifecycle and revalidation

Validation is immutable and setup-time-only:
1. **Plan assembly:** The board profile adapter resolves all connector aliases to
   canonical physical numbers, includes required companion claims, and appends known
   external reservations.
2. **Complete validation:** `km_resource_plan_validate()` checks the entire plan
   while the caller prevents concurrent access or mutation. The function does
   not acquire locks or make validation/application atomic on its own.
3. **Hardware initialization:** Physical peripheral initialization proceeds only if
   validation returns `KM_OK`.
4. **Revalidation rule:** If configuration or device profile changes at runtime, all
   actuator outputs must be brought to a safe stopped state before re-running full
   plan validation. Partial in-place mutation of an active plan is prohibited.

## Example: BoxTurtle / AFC Lite pin conflict resolution

On AFC Lite, `SW1` (connector for DiffA analog signal) is mapped to physical pin `PC4`,
and `SW2` (DiffB) is mapped to `PC5`. These analog signals are routed to `ADC1`
channels 4 and 8:

```c
/* Paired analog sensor claims (owner 10) */
plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = PIN_PC4, .owner = 10 };
plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = PIN_PC5, .owner = 10 };
plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_ADC_UNIT, .instance = 1, .owner = 10 };
plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10 };
plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 8, .owner = 10 };
```

In legacy configurations, `PC4` was aliased as `HUB` switch and `PC5` as `TRG1` switch.
If a legacy profile attempts to register a switch on `PC4`:

```c
plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = PIN_PC4, .owner = 30 };
```

Validation rejects this plan with `KM_ERR_CONFLICT` and `KM_RESOURCE_DIAG_DUPLICATE_RESOURCE`,
identifying the exact indices of both `PC4` claims. The configuration migrator or board
adapter must remap `HUB` and `TRG1` to otherwise unallocated pins before the plan can pass.
