# C4 — Spool linking (Moonraker component ↔ host ↔ Spoolman)

**Version: v1 (2026-09-28).** The host bay↔spool link (H3) and the
Moonraker `openams_spoolman` component (H4/S1–S3) are implemented and
tested end to end.

Status: v1, review resolutions applied.
The evidence is the design (`ARCHITECTURE.md` §4, §5 C4, §6.3-6.4, §7.4; the
linking policy; decisions D2, D3, D5, D6) and the prior art the component is
ported from:

- `afc:` is an AFC_RFID checkout (`AFC_RFID.py`, `AFC_U1_rfid.py`,
  from jimmyjon711/AFC-Klipper-Add-On@bring_in_openams, GPLv3);
- `moonraker:` is `~/moonraker/moonraker/components/spoolman.py`.

Companions: `C2_status.md` (the host fields this reads and the component's inputs),
`C3_commands.md` (the host commands this sends),
`schemas/openams_spoolman_status_v1.schema.json`, `examples/c4/*.json`,
`docs/contracts/tools/gen_examples.py` (generates the examples; owned here).

## 1. Roles

| Party | Owns | Never |
|---|---|---|
| **Host** (`oams_manager`) | the bay ↔ spool link and its source, persisted in the OpenAMS config (D2); tag reads and their decode; lane state | HTTP to Spoolman (ARCHITECTURE §3) |
| **`openams_spoolman`** (Moonraker component, `openams/moonraker/openams_spoolman/`) | the tag → Spoolman policy (resolve, create, link, unlink), active-spool switching, PA application, spool hints, the offline cache, **the spool fact summary** (RESOLVED below) | storing the link itself (it asks the host); motion |
| **Spoolman** | spools, filaments, vendors, weights, extra fields | knowing bays except through the `location` mirror |
| **Moonraker `[spoolman]`** | the HTTP path to Spoolman (`server.spoolman.proxy`), the one active spool, usage reports (`moonraker:282-333`) | — |
| **UIs** | "Assign spool" (manual links), the "New spool, full?" answer, editing a bay's spool facts (§9), the Assign-spool search and per-spool detail (via Moonraker's Spoolman proxy directly) | the tag policy |

The component runs inside Moonraker. It reads the host through a Klipper
subscription to `oams_manager` and talks to Spoolman only through Moonraker's
`[spoolman]` component, never directly (the AFC_RFID precedent:
`afc:AFC_RFID.py:23-30`, `:53-82`).

## 2. Inputs

From the host (`oams_manager` v2, C2):

| Field | Used for |
|---|---|
| `devices.<u>.bays[i].ready` | insertion (rises) and removal (falls) |
| `devices.<u>.bays[i].tag` (`uid` (null for `unit_summary`), `format`, `spoolman_id`, `bambu{…}`, `summary{…}`) | resolution (§4, §4.1) |
| `devices.<u>.bays[i].spool_id`, `.link_source` | the current link; manual links made by a UI |
| `lanes_by_fps.<l>.op`, `.unit`, `.runout`, `.reload_target`, `.extruder` | active-spool switching and PA (§7) |
| `settings.auto_create_spoolman_spools`, `settings.apply_pa_on_load` | policy (D3) |
| `settings.sync_unit_tray_info`, `devices.<u>.capabilities.tray_write` / `.tray_info`, `devices.<u>.bays[i].positioning` | a unit's stored filament setting (§7.5) |
| `ready` | take no action while the manager is not ready |

From Spoolman, through the proxy (§5). From UIs: the pending-confirmation answer
(§3, resolved: a component endpoint, see §5.8).

## 3. Per-bay linker states

States (ARCHITECTURE §7.4): `unlinked` → `resolving` → `linked` (with a source) →
`pending_confirmation`. The component keeps one machine per bay, keyed
`"<unit>-<bay>"` (the `filament_group` convention). Its status publishes each
bay's state (§8).

| From | Event | Condition | To | Component does |
|---|---|---|---|---|
| any | component start or manager `ready` | the host holds a link for the bay | `linked` (host's source) | nothing; the host's persisted link is authoritative |
| `unlinked` | tag read, `format: spoolman_link` | the id resolves in Spoolman, or Spoolman is offline | `linked` (tag) | `OAMSM_SET_BAY_SPOOL … SPOOL=<id> SOURCE=tag`; mirror `location` (§5.6) |
| `unlinked` | tag read, `format: bambu` | — | `resolving` | look up (§4) |
| `resolving` | a spool matches `tray_uid` or a tag UID | — | `linked` (tag) | `OAMSM_SET_BAY_SPOOL`; mirror `location`; add the UID if new (§5.7) |
| `resolving` | no match | `auto_create_spoolman_spools`, online | `pending_confirmation` | create vendor, filament, spool (§5.3-5.5); `OAMSM_SET_BAY_SPOOL … SOURCE=tag` |
| `resolving` | no match | creation off | `unlinked` | `error`: no Spoolman spool for this tag; UIs offer Assign spool |
| any | tag read, `format: unit_summary` | — | by §4.1: unchanged (Keep), `linked` (tag), `unlinked` with suggestions, or `pending_confirmation` (Create) | the Keep / Match / Create policy of §4.1; never a guess |
| `resolving` | Spoolman offline | the cache knows the tag | `linked` (tag) | `OAMSM_SET_BAY_SPOOL` from the cache; queue the `location` mirror (§6) |
| `resolving` | Spoolman offline | the cache does not know the tag | `resolving` | queue the creation (§6); `error` says so |
| `pending_confirmation` | the display calls the confirmation endpoint (§5.8) | — | `linked` (tag) | the component PATCHes `remaining_weight` and clears `pending_confirmation` itself (RESOLVED, C4 4: the component stays the only writer of its own state) |
| `linked` (tag) | a UI assigns another spool (host `link_source` becomes `manual`) | — | `linked` (manual) | mirror `location` for the new spool, clear it on the old one |
| `linked` (manual), tagged bay | a re-read is requested | — | `resolving` | re-apply the tag (manual wins until re-read: gap analysis step 5) |
| `linked`, `pending_confirmation` | removal: `ready` falls **and** the tag is gone | — | `unlinked` | `OAMSM_CLEAR_BAY_SPOOL`; clear `location` |
| `linked` (tag), `pending_confirmation` | `ready` true and the bay's tag disappears (a re-read found no tag) | — | `unlinked` | `OAMSM_CLEAR_BAY_SPOOL`; clear `location` |
| any | tag read with `format` `unknown`, or OpenSpool without an id | — | unchanged | nothing; UIs offer Assign spool |
| any | `OAMSM_SET_BAY_SPOOL` names a spool already linked to another bay | — | `linked` on the new bay | **RESOLVED (C4 13): move.** The component clears the old bay's link (`OAMSM_CLEAR_BAY_SPOOL`) and reports the move in its response, rather than refusing |

Two rules follow from the hardware:
- **Runout is not removal.** When a bay runs out, `ready` falls but the empty spool
  and its tag stay in the bay (`examples/c2/oams_manager_runout_reload.json`). Only
  "`ready` false and tag gone" unlinks. The OpenAMS 1 reader debounces removal over two
  missed polls (`src/mfrc522.py:449-461`).
- **An untagged manual link has no observable removal**, since its `ready` fell at
  the runout already. It is kept while the lane is in a runout or the bay is
  loaded, loading or unloading; cleared on the first empty read afterwards (the
  empty spool's identity is not retained). **RESOLVED (F-4).**

Timing: H4's acceptance is a Bambu spool linked or created within 5 s of insertion.

## 4. Resolution (tag first)

From the linking policy and ARCHITECTURE §6.4:

1. **Spoolman-link tag** (`tag.format == "spoolman_link"`, NDEF `web+spoolman:s-<id>`):
   link `tag.spoolman_id`.
2. **Bambu tag** (`tag.format == "bambu"`): find the spool whose `tray_uid` extra
   equals `tag.bambu.tray_uid` (both tags of one Bambu spool share it, so either
   side finds it), else whose `card_uids` extra contains the UID. Spoolman offers no
   query on extra fields; like AFC_RFID, the component matches over
   `GET /v1/spool` results and its cache (`afc:AFC_RFID.py:122-128`, `:1242-1292`).
   Found: link. Not found: create (§5.3-5.5) when `auto_create_spoolman_spools`.
3. **Unit summary** (`tag.format == "unit_summary"`, no UID): the Keep / Match / Create policy of §4.1, which never links ambiguously.
4. **No tag, unreadable, or unknown format:** nothing automatic. UIs offer Assign
   spool, which sends `OAMSM_SET_BAY_SPOOL … SOURCE=manual` to the host.
5. **Removal:** §3.
6. **Manual on a tagged bay:** manual wins; UIs warn "tag says X"; a re-read
   re-applies the tag.

The Bambu decoder reports `manufacturer: "Bambu"` (`src/bambu_lab_tag_processor.py:69`);
the Spoolman vendor is **"Bambu Lab"**.

**RESOLVED (review, C4 8):** a queued creation that meets a spool
created meanwhile (another tool, the web UI) must de-duplicate by `card_uids`
when the write queue replays (H4), rather than creating a second spool for the
same physical tag.

### 4.1 Unit-decoded summaries (`format: "unit_summary"`)

A unit that decodes its own tags (C1 §4.7.1) publishes a **summary** — filament
code, material, colour, nozzle range — and **no UID, no tray UID**. Two spools
of the same filament are then indistinguishable, so **a spool cannot be
identified uniquely, and the component never auto-links ambiguously**: it links
only when exactly one spool can be the one, and otherwise leaves the choice to
the user. A read is actionable when the summary has a `material`.

The component keeps §3's state machine; only the resolution differs. On
each new read (a new `read_at`; the UID is null, so `(uid, read_at)` changes
with `read_at` alone):

1. **Keep.** If the bay already has a link, of any source, and the summary
   matches that spool's filament, nothing changes: no command, no write, and
   `pending_confirmation` stays. A filament matches when its **material**
   equals the summary's (case and spaces ignored), its **colour** equals the
   summary's `color_argb & 0xFFFFFF` when the summary has one (none matches
   any colour), and its **vendor** is "Bambu Lab" when the summary's
   `manufacturer` is `"Bambu"` (an unknown manufacturer constrains nothing).
   The filament code is not stored in Spoolman and is not compared. With
   Spoolman offline and the linked spool never read, the link is kept. A link
   that does **not** match is stale (the spool in the bay was changed): the
   read goes on to Match, and if that links no spool the stale link is cleared
   (`OAMSM_CLEAR_BAY_SPOOL`, `location` cleared).
2. **Match.** Otherwise the candidates are the non-archived Spoolman spools
   whose filament matches the summary and that **no other bay holds** (neither
   the host's links nor another bay's machine). A candidate whose `location`
   equals this bay's key (`"<unit>-<bay>"`) is preferred: when any exist, only
   those remain. If **exactly one** candidate remains, link it
   (`SOURCE=tag`, §5.6 mirror). If **several** remain, the bay goes to
   `unlinked`, `error` says how many spools match, and the bay's
   **`suggestions`** (§8) lists them (at most ten) for a UI to offer; the user's
   choice arrives as an ordinary manual link (`OAMSM_SET_BAY_SPOOL …
   SOURCE=manual`), which ends the suggestions. Nothing is created and nothing
   is guessed.
3. **Create.** With no candidate: when `auto_create_spoolman_spools` is on and
   Spoolman is online, create the vendor ("Bambu Lab" for `manufacturer:
   "Bambu"`, else "Generic"), the filament (found by vendor, material and
   colour, else created, with `settings_extruder_temp` the mean of the nozzle
   range when it has one) and the spool (§5.5, but without `tray_uid` and
   without `card_uids`), link it, and enter **`pending_confirmation`**: the
   summary carries no weight, so the spool starts at its filament's default
   `weight` (1000 g if the filament has none) and the user confirms how full it
   is (§5.8). With creation off: `unlinked`, `error`: no Spoolman spool for
   this tag; UIs offer Assign spool.
4. **Offline.** With Spoolman offline the component links from the cache,
   whose key is **the bay and the summary** (`sum:<unit>-<bay>:<code>|<material>|<colour>`;
   written whenever a summary resolves to a spool online), with the location
   mirror queued (§6). A miss stays `resolving`: with creation on the creation is
   queued (one per bay), and **replayed through the whole policy**, so a spool
   that appeared meanwhile is matched, not duplicated; a replay whose read is no
   longer the bay's current one creates nothing.

Removal (§3) is unchanged: `ready` falling with no runout, or a re-read that
finds no tag (the host publishes no `tag` for "read, no tag" or "read failed"),
unlinks a tag-sourced link.

**A setting the component wrote (§7.5).** A unit that stores a per-bay setting
reports it as this same summary. When a read's summary is the setting the
component last wrote into that bay (same code, material and colour; one record
per bay, persisted with the cache, §6), then: if the bay is linked to the spool
it was written from, the read is the unit's **own echo of the write**, not a new
spool, and nothing happens at all (no Keep check, no command); otherwise Keep
and Match run **without the manufacturer**, since a code the component chose
(Spoolman's or the default table's) says nothing about who made the spool.
Without this, a written generic code such as `GFL99` would name Bambu and
unlink a spool of any other vendor on its own echo.

**Why.** Every other tag identifies one physical spool (a `tray_uid`, a tag UID
or an explicit Spoolman id), so a match can be trusted. A summary identifies a
*kind* of filament. Linking the first of several look-alike spools would
silently attribute usage and remaining weight to the wrong one, which is worse
than asking: so the component links only on a unique answer, prefers the spool
the user already located at the bay, and otherwise offers the candidates.

## 5. What the component writes to Spoolman

All requests go through Moonraker's proxy (`POST /server/spoolman/proxy`, JSON-RPC
`server.spoolman.proxy`) with `{"request_method", "path", "query"?, "body"?}`;
`path` starts with `/v1/`; a GET carries no body (`moonraker:342-394`). `body` must
be a JSON **object**, not a pre-serialized string, or Spoolman answers 422
(`afc:AFC_RFID.py:64-72`). Extra-field values are JSON-encoded strings
(`"55"`, `"\"text\""`, `afc:AFC_RFID.py:230-231`), and a PATCH of `extra` must send
the whole merged `extra` object, as AFC_RFID does (`afc:AFC_RFID.py:286-305`).

### 5.1 Conventions

**RESOLVED (review, global and C4 1/2/6/7): Spoolman field names
follow AFC_RFID exactly**, not the earlier draft's invented names. The reason
OpenAMS follows AFC_RFID rather than writing a new schema is so a spool is
recognized across the Snapmaker U1, AFC, and OpenAMS — which only AFC_RFID's
actual keys achieve.

| Key | On | Spoolman type | Value | Written | Prior art |
|---|---|---|---|---|---|
| `card_uids` (extra) | spool | text | comma-separated uppercase-hex tag UIDs, sorted; a spool can carry several | on create, and when a known spool shows a new UID | AFC_RFID's own key and encoding, unchanged (`afc:AFC_RFID.py:233-236`, `:339-345`, `:727-734`). **Was `tag_uids` in the earlier draft; corrected.** |
| `tray_uid` (extra) | spool | text | `tag.bambu.tray_uid` as the host publishes it (32 lowercase hex digits, `src/bambu_lab_tag_processor.py:85`) | on create | none (new; AFC_RFID has no Bambu-specific tray concept) |
| `location` (built-in) | spool | string | `"<unit>-<bay>"`, e.g. `"oams1-0"` (D2) | on link; **cleared to `""` on unlink (RESOLVED, C4 3)** | none: AFC_RFID never writes `location` |
| `lot_nr` (built-in) | spool | string | the tag's manufacturing date, normalized by **AFC_RFID's normalizer** (RESOLVED, C4 6): handles `YYYYMMDD` and ISO dates, passes anything else through (`afc:AFC_U1_rfid.py:976-992`). The Bambu `production` string (`src/bambu_lab_tag_processor.py:86`, e.g. `"230115"`) is 6-digit `YYMMDD`; the normalizer's `YYYYMMDD` path does not match it as-is, so the component front-pads the century (`20` + the 6 digits) before normalizing | on create | `afc:AFC_RFID.py:316-348`, `afc:AFC_U1_rfid.py:976-992` |
| `initial_weight`, `remaining_weight` (built-in) | spool | number, g | `tag.bambu.weight_g`; a new spool starts full | on create | AFC_RFID ignores the tag weight and writes 1000 g (`afc:AFC_RFID.py:1346`, `:1525-1526`); this design uses the tag's actual weight since OpenAMS decodes it |
| `flow_k` (extra) | **spool** | float | AFC_RFID's per-spool calibrated flow/PA constant | never written by the component; read to apply PA (§7.2) | AFC_RFID's actual key and level (`afc:AFC_RFID.py:232`, `:359-390`). **Was filament extra `pressure_advance` in the earlier draft; corrected — PA is a per-spool value here, following AFC_RFID exactly, not a per-filament one** |
| `drying_temp` (extra) | filament | integer | °C, `tag.bambu.drying_temp_c` | on create | none (new; AFC_RFID has no drying-preset extra) |
| `drying_time` (extra) | filament | integer | hours, `tag.bambu.drying_time_h` | on create | none (new) |
| `filament_code` (extra) | filament | text | the manufacturer's filament code, for example a Bambu filament code such as `GFB60`: what a unit that stores a per-bay setting is given as `code` (§7.5). Up to 6 printable ASCII characters are used; anything else is ignored | never written by the component; set by the user | none (new) |
| `variant` (extra) | filament | text | **AFC_RFID's behavior (RESOLVED, C4 7):** the tag's sub-type text, unmodified. Bambu tags carry `type` (e.g. "PLA") and `detailed` (e.g. "PLA Basic") but no separate sub-type field; the component writes `detailed` verbatim, the same source AFC_RFID uses when it has one | on create | `afc:AFC_RFID.py:238-239`, `:286-305` |
| vendor `name` | vendor | — | `"Bambu Lab"` for Bambu tags | get-or-create | AFC_RFID takes the vendor from the tag (`afc:AFC_U1_rfid.py:949-951`); OpenAMS's Bambu decoder does not carry a vendor field, so the component hard-codes "Bambu Lab" for `tag.format == "bambu"` |

The integer field type is not exercised by AFC_RFID, which uses only `text` and
`float`.

### 5.2 Extra-field bootstrap (once per component start)

```
GET  /v1/field/spool            then, for each missing key:
POST /v1/field/spool/card_uids         {"name": "Card UIDs", "field_type": "text"}
POST /v1/field/spool/tray_uid          {"name": "Tray UID", "field_type": "text"}
POST /v1/field/spool/flow_k            {"name": "Flow K", "field_type": "float"}
GET  /v1/field/filament         then, for each missing key:
POST /v1/field/filament/drying_temp    {"name": "Drying temperature", "field_type": "integer"}
POST /v1/field/filament/drying_time    {"name": "Drying time", "field_type": "integer"}
POST /v1/field/filament/variant        {"name": "Variant", "field_type": "text"}
POST /v1/field/filament/filament_code  {"name": "Filament code", "field_type": "text"}
```
Pattern and idempotency as `afc:AFC_RFID.py:241-284` (a POST is create-or-update,
so a race is harmless). The display `name` strings are proposals.

### 5.3 Vendor

```
GET  /v1/vendor?name=Bambu%20Lab      -> [] ?
POST /v1/vendor {"name": "Bambu Lab"}
```
(`afc:AFC_RFID.py:139-147`)

### 5.4 Filament

Find: `GET /v1/filament?vendor.name=Bambu%20Lab&material=<type>`, then match
`name == tag.bambu.detailed` and `color_hex == RRGGBB` client-side
(`afc:AFC_RFID.py:104-114`). Create when none matches (`afc:AFC_RFID.py:176-196`):

```json
POST /v1/filament
{"name": "PLA Basic", "vendor_id": 3, "material": "PLA", "density": 1.24,
 "diameter": 1.75, "color_hex": "0A2989", "weight": 1000,
 "settings_extruder_temp": 210, "settings_bed_temp": 35}
PATCH /v1/filament/<id>
{"extra": {"drying_temp": "55", "drying_time": "8", "variant": "\"PLA Basic\""}}
```

- `color_hex` = `tag.bambu.color_argb & 0xFFFFFF` as six uppercase hex digits;
- `density` from AFC_RFID's material table (`afc:AFC_RFID.py:598-634`, `:834-852`);
- `settings_extruder_temp` = the mean of `hotend_min_c` and `hotend_max_c`, as
  AFC_RFID computes it (`afc:AFC_U1_rfid.py:944-957`);
- `settings_bed_temp` = `tag.bambu.bed_temp_c` when non-zero, else AFC_RFID's
  per-material fallback (`afc:AFC_RFID.py:938-962`).

### 5.5 Spool

```json
POST /v1/spool
{"filament_id": 17, "initial_weight": 1000, "remaining_weight": 1000}
PATCH /v1/spool/<id>
{"lot_nr": "2024-05-12", "location": "oams1-2",
 "extra": {"card_uids": "\"55EE66FF\"", "tray_uid": "\"00ff11ee22dd33cc44bb55aa66997788\""}}
```
(the create-then-PATCH pattern of `afc:AFC_RFID.py:211-225`, `:326`). The bay's
state becomes `pending_confirmation` (a Bambu tag stores no remaining amount).

### 5.6 Link and unlink mirror

```json
PATCH /v1/spool/<id> {"location": "oams1-2"}      on link
PATCH /v1/spool/<id> {"location": ""}             on unlink (RESOLVED, C4 3)
```

### 5.7 A known spool shows a new tag UID

```json
PATCH /v1/spool/<id> {"extra": {<existing extra…>, "card_uids": "\"11AA22BB,33CC44DD\""}}
```

### 5.8 Confirmation

**RESOLVED (review, C4 4):** the display calls a component endpoint
(`POST /server/openams_spoolman/confirm` — see §8 naming), not Spoolman directly.
The component then does the write itself:

```json
PATCH /v1/spool/<id> {"remaining_weight": 750}
```
Full, ¾, ½, ¼, or grams (gap analysis, display WP D4), and the component clears
`pending_confirmation` in its own status in the same step — so the component
stays the only writer of its own state, and a display that never answers does
not leave the flag in a state the component itself did not set.

**Not written by the component:** usage. Moonraker reports the active spool's
consumption itself (`PUT /v1/spool/<id>/use`, `moonraker:296-333`).

## 6. Offline behavior and cache

ARCHITECTURE §7.4: when Spoolman is offline, links still reach the host from the
cache, creations are queued, the status says `spoolman_online: false`, and nothing
blocks printing.

| Part | Behavior | Prior art |
|---|---|---|
| **Read cache** | tag key → `{spool_id, filament summary}`, keys `uid:<UID>` and `tray:<tray_uid>`; pre-warmed by one full `GET /v1/spool` scan on the first contact with Spoolman; refreshed on every successful resolution | `afc:AFC_RFID.py:1083-1292` (keys `uid:`/`sku:`, one-shot pre-warm, write-back on every hit) |
| **Write queue** | creations, `location` mirrors, and UID additions are queued in order and replayed when Spoolman returns; the depth is `queued_writes`; a replayed creation de-duplicates by `card_uids` against what Spoolman now holds (RESOLVED, C4 8) | **new**: AFC_RFID has no replay queue and skips writes while offline (`afc:AFC_RFID.py:1358-1364`) |
| **Reachability** | Moonraker's `spoolman_connected`, with `notify_spoolman_status_changed` (`moonraker:89-91`, `:396-411`), or a `GET /v1/info` probe | `afc:AFC_RFID.py:84-94` |
| **Active spool** | works offline: Moonraker's active spool is local and its usage reports queue in Moonraker | `moonraker:282-294` |
| **Storage** | persisted by the component (a Moonraker database namespace or a file; not a contract surface) | `afc:AFC_RFID.py:1106-1192` (a JSON file, atomic replace) |

## 7. What the component sends to the host and to Moonraker

### 7.1 Host commands (C3)

| Command | When | C3 status |
|---|---|---|
| `OAMSM_SET_BAY_SPOOL OAMS=<unit> BAY=<n> SPOOL=<id> SOURCE=tag` | a tag resolved or a spool was created | **EXISTS (H3, 2026-09-28):** `src/oams_manager.py:1785`; persisted as `spool_id_<bay>`/`link_source_<bay>` |
| `OAMSM_CLEAR_BAY_SPOOL OAMS=<unit> BAY=<n>` | removal (§3), or the "move" case (§3 last row) clearing the old bay | **EXISTS (H3, 2026-09-28):** `src/oams_manager.py:1824` |
| a spool-summary and hint command | **RESOLVED (C2 4/7): removed.** The host no longer carries the Spoolman summary at all (it lives only in the component's own status, §8), so no command needs to push it into `oams_manager`. `spool_hint` (D6, §7.3) is the one Spoolman-derived value the host still needs, and it travels on its own (below) |
| a `spool_hint` command (name TBD) | after a link, and whenever the spool's remaining weight changes | **FUTURE (not in v1):** D6 geometry reserved; C3 §6 |
| `OAMSM_SET_BAY_FILAMENT OAMS=<unit> BAY=<n> [MATERIAL=<text>] TEMP=<int> SOURCE=spool\|table` | when a bay's material or working temperature changes (§7.4) | **EXISTS (H2):** `src/oams_manager.py` `cmd_SET_BAY_FILAMENT`; memory only, never persisted |
| `OAMSM_SET_TRAY_INFO OAMS=<unit> BAY=<n> [CODE=<code>] MATERIAL=<text> COLOR=<RRGGBBAA> MIN=<C> MAX=<C>` | when a bay's unit should store the linked spool's filament (§7.5), or on request through the remote method below | **EXISTS:** `src/oams_manager.py` `cmd_SET_TRAY_INFO`; the unit keeps it |

Manual links reach the host from UIs directly (`OAMSM_SET_BAY_SPOOL … SOURCE=manual`);
the component only observes them. **Preconditions (RESOLVED, C3 3):**
`OAMSM_SET_BAY_SPOOL`/`OAMSM_CLEAR_BAY_SPOOL` are allowed at any time, including
on a loaded bay; a change on the loaded bay triggers the active-spool update
through this component (§7.2), same as a load/unload would.

### 7.2 Active spool and PA

Moonraker attributes usage to exactly one active spool (`moonraker:282-294`). The
component calls `set_active_spool(<id>)` or `set_active_spool(None)` in process
(the same function Moonraker's `spoolman_set_active_spool` remote method reaches,
`moonraker:72-74`, and that the `SET_ACTIVE_SPOOL` / `CLEAR_ACTIVE_SPOOL` macros
call; Moonraker has no separate clear, `None` clears). Moonraker then emits
`notify_active_spool_set {"spool_id": …}` (`moonraker:89-91`, `:291-293`).

| Host event (C2) | Active spool |
|---|---|
| a lane's `op` becomes `LOADED` | the loaded bay's `spool_id` (`None` if unlinked, so usage never lands on a stale spool) |
| a lane's `unit` changes while `LOADED` (runout reload success, `src/oams_state.py:632-639`) | the new bay's `spool_id` (ARCHITECTURE §6.3 step 6) |
| a lane's `op` leaves `LOADED` for `UNLOADED` | `None` |
| the loaded bay's link changes | the new `spool_id` |

With `settings.apply_pa_on_load`, on the first two events: when the linked
spool's `flow_k` extra is set, run `SET_PRESSURE_ADVANCE
EXTRUDER=<lanes_by_fps.<l>.extruder> ADVANCE=<value>` (ARCHITECTURE §6.3 step 4;
**RESOLVED, C4 1/2: `flow_k` is a spool extra, read per the currently-linked
spool, not a filament-level `pressure_advance`**).

Filament still in the path from the old spool at the moment of a runout switch is
attributed to the new spool. The maximum error per runout is bounded by the
toolhead inlet-to-nozzle length (order 50–150 mm, or 0.15–0.45 g of 1.75 mm PLA)
plus at most one status period of extrusion, in one direction only. There is no
double counting: Moonraker's own status handler runs before `set_active_spool`,
which rebases `_highest_epos`. **RESOLVED (F-4): this error is within H4's
"usage splits correctly" acceptance.**

**RESOLVED (review, C4 10):** with several lanes loaded, follow
the lane whose `extruder` matches `toolhead.extruder` (the printer's
currently-selected extruder), not an arbitrary or first-loaded lane.

### 7.3 `spool_hint` (D6, reserved)

For each linked bay the component computes the spool's fill (mass g, outer radius
mm, valid flag) from Spoolman's remaining weight, the filament density and
diameter, and the spool's empty weight, and forwards it to the host (§7.1), which
publishes it (`bays[i].spool_hint`, C2) and sends it to the firmware on load and
when it changes (C1). v1 carries the hook; the firmware may ignore it. **RESOLVED
(review, C4 11): the spool geometry (outer radius) is reserved,
future work** — Spoolman does not store it (Bambu tags carry `spool_width_mm`
only, not a core/outer radius), so `valid` stays `false` in v1 unless a future
geometry source is added.

### 7.4 Each bay's material and working temperature (H2)

The macros heat the nozzle before an unload, a load or a toolchange, so they
need each bay's working temperature. **Spoolman stays the store; the host
holds only an operating copy, in memory only** (`bays[i].filament`,
C2 §4.5.1), pushed here with `OAMSM_SET_BAY_FILAMENT` (C3).

**How the temperature is chosen.** For a linked bay, first match wins:

1. **The spool's own value** — its filament's `settings_extruder_temp` in
   Spoolman, `source: "spool"`. Tag-created filaments already carry the tag's
   average (§5.4), so this is usually present.
2. **The material table**, `source: "table"` — beside `MATERIAL_BED_TEMP`
   (`ops.py` `MATERIAL_EXTRUDER_TEMP`), in °C:

   | | | | |
   |---|---|---|---|
   | PLA 210 | PLA-CF 220 | PETG 240 | PETG-CF 250 |
   | ABS 250 | ASA 255 | HIPS 240 | PC 270 |
   | PA 270 | PA-CF 280 | PA6 275 | TPU 225 |
   | TPE 225 | PVA 200 | PP 235 | PPS 320 |

   **Lookup:** the material, upper-cased and trimmed, matched exactly first;
   then the part before the first `-`, `+` or space, so `PLA+` and `ABS-GF`
   fall back to `PLA` and `ABS`.

   **User overrides:** the optional `material_temps` option of the component's
   `[openams_spoolman]` section, `NAME=TEMP` entries separated by commas or
   new lines (`material_temps: PLA=215, PETG=235`). It **replaces or adds**
   individual entries; everything it does not name keeps the built-in value.
   An entry that does not parse (not an integer, or outside 150-400) is
   **logged and skipped** — one typo never stops the component. The option is
   read once, in the component's constructor.
3. **Unknown otherwise** — an unlinked bay, a spool with no material, or a
   material not in the table. The component then sends the bare command, which
   clears the bay, and the macros use their own default. A spool's own value
   that the host could not hold (outside 150-400, not a number) is treated the
   same way and falls through to the table.

The material pushed is the filament's `material` as Spoolman has it (trimmed,
at most 16 characters), or none.

**When it is pushed.** On every status pass the component works out each bay's
`(material, temp_c, source)` from the bay's link and the cached spool summary
(§6) and compares it with what it last sent for that bay; it sends only when
they differ. A bay that becomes unlinked is cleared once, and only when
something was sent before. **After a Klipper restart the host has forgotten
everything**, so `_handle_klippy_ready` tells the linker to forget what it sent
and the next pass pushes every bay again.

**Spoolman offline** the cached summary is used (§6); with no cached summary
nothing new is sent and the bay stays unknown. A G-code Klipper refuses is
logged and not recorded, so it neither breaks the pass nor silences the
retry.

### 7.4.1 Slicer filament list (`lane_data`)

OrcaSlicer's "sync filaments from printer" reads Moonraker's database
namespace `lane_data` (`GET /server/database/item?namespace=lane_data`), the
layout AFC publishes. The component keeps it in step with each tool. Option
`publish_lane_data` in `[openams_spoolman]` (default `True`; `False` writes
nothing and registers no namespace).

- One lane per host group, in tool order, under the key `lane<N>` with
  N = tool number + 1. The tool number is the digits ending the group name
  (`T3` is 3), else the group's position.
- Each lane is `{"lane": "<tool number, a string>", "color": "#RRGGBB",
  "material", "bed_temp", "nozzle_temp", "name", "vendor", "spool_id"}`.
- The bay that represents a group is the bay it has loaded on its lane, else
  its `next_bay`. Its linked spool supplies material, colour, name, vendor and
  id. `nozzle_temp` follows §7.4 (spool value, then the material table, then
  the group's `next_filament_temp_c`); `bed_temp` is the spool's
  `settings_bed_temp`, else `MATERIAL_BED_TEMP`, else 0.
- No bay or no linked spool: the lane is published with `material: ""`.
- A lane is written only when its content differs from what the namespace
  holds (read once after the component starts); lanes of groups that no
  longer exist are deleted. A database error is logged once and never fails
  the status pass.

### 7.5 A unit's stored filament setting

Some units store, per bay, the filament they report for a spool whose tag they
do not read, such as a cardboard spool (`capabilities.tray_write`, C1 §4.7.2),
the way a printer lets the user pick a filament for a tray. **Spoolman is the
source of truth**: with `settings.sync_unit_tray_info` on (the default), the
component writes the linked spool's filament into the unit with
`OAMSM_SET_TRAY_INFO` (C3 §4) when the two differ. `tray.py` holds the mapping
and the policy.

**The setting** (`tray.setting_for`), from the cached spool summary (§6):

| Field | From the spool | Otherwise |
|---|---|---|
| `code` | the filament's `filament_code` extra (§5.1), upper-cased, when it is 1-6 printable ASCII characters without spaces | the unit's own code while the material is unchanged, else the table below |
| `material` | the filament's `material`, printable ASCII only, upper-cased, the first 8 characters | none: a filament without a material is never written |
| `color_rgba` | the filament's `color_hex` as `0xRRGGBBFF` (an 8-digit `RRGGBBAA` keeps its alpha) | the unit's own colour while the material is unchanged, else `0` (no colour) |
| `nozzle_min_c`, `nozzle_max_c` | the filament's `settings_extruder_temp` ±10 °C, kept within 150-400, when it is a temperature the host accepts (§7.4) | the unit's own range while the material is unchanged, else the table below |

**The default table** (`tray.MATERIAL_DEFAULTS`, the one place it is kept) is a
**best-effort default**: Bambu's generic filament codes and a common nozzle
range. Looked up like §7.4's table: the exact material, then the part before
the first `-`, `+` or space.

| Material | Code | Nozzle °C |
|---|---|---|
| PLA | GFL99 | 190-230 |
| PLA-CF | GFL98 | 190-230 |
| PETG | GFG99 | 220-260 |
| ABS | GFB99 | 240-280 |
| ASA | GFB98 | 240-280 |
| TPU | GFU99 | 200-240 |
| PA | GFN99 | 260-300 |
| PC | GFC99 | 260-300 |
| PVA | GFS99 | 190-230 |

A material the table does not know takes `GFL99`, and a range from §7.4's
extruder table ±10 °C (with the `material_temps` overrides), else PLA's.
Keeping the unit's own code and range for an unchanged material means a write
never replaces a more specific code (for example a vendor's own) with a generic
one, and a default never causes a write by itself.

**When it is written** (`tray.TraySync`, on every status pass, per bay):

- **Eligible bays only:** the unit has `capabilities.tray_write` and
  `tray_info`, the setting is on, the bay is `ready` and linked, the bay's tag
  is the unit's own `unit_summary` or none, and the spool is not one Spoolman
  knows by a tag (`tray_uid` or `card_uids`, which the unit reads itself). A
  tag is authoritative: nothing is ever written for it. An empty bay is never
  written.
- **On a Spoolman-side change only:** a new link, a re-link, or a change of
  the spool's material, colour, `filament_code` or `settings_extruder_temp`
  (an edit through §9 at once; an edit made in Spoolman itself when the
  component re-reads the linked spool, every 60 s). A link that the unit's own
  report made (`link_source: tag`, §4.1) is taken as already in step: the unit
  holds the filament that decided it, so it is written only after the spool
  changes.
- **Only when different:** the setting is compared with the bay's current
  summary (code, material, colour and range); equal means nothing to do. No
  summary at all (no tag read) is different.
- **Debounced and bounded:** a change waits 2 s (and while the bay is
  `positioning`), then is written; while the unit still reports something
  else the write is repeated 30 s later, at most **3 times per change**, then
  the bay shows `failed` until the spool changes again. A refusal the host
  reports as `invalid_field` (C1 §4.7.2) is never repeated. A setting the unit
  holds after it was in step is left alone (`kept`) until the spool changes,
  so a deliberate manual write is not undone.
- **No loop:** before each write the component records it per bay (§4.1, "A
  setting the component wrote"), so the unit's re-delivered `ams_tray_info` is
  neither a new spool for the linking flow nor a new change for this policy.

The component asks Moonraker for a pass when a debounce or a retry is due,
since the host's status may not change meanwhile.

**On request:** `OAMSM_SET_TRAY_INFO OAMS=<unit> BAY=<n> [SPOOL=<id>]` (C3 §4)
calls the component's remote method `openams_spoolman_tray_write` (`unit`,
`bay`, `spool_id`), which reads the spool, maps it against the bay's current
summary, records the write and sends the explicit form, whatever the sync
policy says. A failure on the component's side goes to the console.

## 8. The component's status object

`schemas/openams_spoolman_status_v1.schema.json`. **RESOLVED (design
review, C2 4/7): this object now also carries the spool summary** that used to
be proposed for `oams_manager`/`openams_ui` (material, color, vendor,
remaining), per bay, since Spoolman — not the host — owns those facts:

```json
{"schema_version": 1, "spoolman_online": true, "edit": true, "stop": true,
 "queued_writes": 0,
 "bays": {"oams1-2": {"state": "pending_confirmation", "spool_id": 21,
                      "source": "tag", "pending_confirmation": true, "error": null,
                      "spool": {"material": "PETG", "color_hex": "000000",
                                "vendor": "Bambu Lab", "remaining_g": 1000,
                                "remaining_pct": 100, "initial_g": 1000,
                                "flow_k": null}}}}
```

| Key | Note |
|---|---|
| `spoolman_online` | ARCHITECTURE §5 C4 |
| `edit` | `true` when the component serves the edit endpoint (§9); additive, so a UI that finds it absent or false hides its edit controls |
| `stop` | `true` when the component serves the stop route (§9.1); additive, so a UI that finds it absent or false hides its Stop button's sequence stop |
| `queued_writes` | the write queue's depth (§6) |
| `bays.<unit>-<bay>.state` | §3 |
| `.spool_id`, `.source` | the link as the component last applied it; the host's `bays[i].spool_id`/`link_source` is authoritative |
| `.pending_confirmation` | true exactly in `pending_confirmation` (the schema enforces it) |
| `.error` | human text for the last failure, else null |
| `.tray_sync` | **additive, optional (§7.5).** Present only on a bay §7.5 keeps in step: `{state, error}`, `state` one of `in_sync` (the unit holds the spool's filament), `pending` (a write waits, or was sent and its report is awaited), `kept` (the unit holds another setting that is left alone: it decided the link, or it changed after the bay was in step, and the spool has not changed since), `failed` (the writes for the spool's latest change did not take); `error` the last write's failure, else null |
| `.suggestions` | **additive, optional (§4.1).** Present only on an `unlinked` bay whose unit-decoded tag matches several spools: up to ten `{spool_id, material, color_hex, vendor, remaining_g, location}` entries a UI may offer. Absent otherwise |
| `.spool` | the linked spool's summary (material, color_hex, vendor, remaining_g, remaining_pct), or null when unlinked/resolving/not yet known — the display merges this with `openams_ui <unit>` (C2 §6) |
| `.spool.initial_g` | the spool's `initial_weight`, else the filament's nominal `weight`, else null; a UI prefills the edit form's initial weight from it (§9). Additive |
| `.spool.flow_k` | the spool's `extra.flow_k` pressure advance, decoded exactly as §7.2 applies it, so a value that is not a number is null; a UI prefills the edit form's pressure advance from it (§9). Additive |

The summary the component caches also carries the spool's own
`settings_extruder_temp` as `extruder_temp`, which §7.4 works from. It is a
working value, not a §8 field: it appears in this object only when the spool
carries a temperature, so the schema's named keys stay exact. Two more working
values serve §7.5 the same way, each present only when there is one to say:
`filament_code` (the filament's extra) and `tagged: true` (the spool has a
`tray_uid` or `card_uids`).

The cache (§6) also keeps, per bay, the filament setting the component last
wrote into the unit and the spool it came from (§4.1, §7.5).

Served as a Moonraker endpoint and pushed as a notification on every change.
**RESOLVED (review, C4 12): the draft's proposed names are
accepted:** `GET /server/openams_spoolman/status`
(`server.openams_spoolman.status`), `POST /server/openams_spoolman/confirm`
(the §5.8 endpoint), `POST /server/openams_spoolman/edit` (§9), and
`notify_openams_spoolman_status`, registered with an
explicit notification name (Moonraker's default-from-event-suffix pattern,
e.g. `spoolman:active_spool_set` → `notify_active_spool_set`,
`moonraker:89-91`, would be too generic here).

Examples: `examples/c4/openams_spoolman_linked.json`, `_resolving.json`,
`_pending_confirmation.json`, `_offline.json`; regenerate with
`docs/contracts/tools/gen_examples.py` after any schema change.

## 9. Editing a spool from a UI

A UI sets a bay's material, color, vendor, weights and pressure advance through
one component endpoint, so nobody has to edit Spoolman by hand. The status object
advertises it with `"edit": true` (§8).

**JSON-RPC** `server.openams_spoolman.edit` (HTTP
`POST /server/openams_spoolman/edit`).

| Param | Type | Rule |
|---|---|---|
| `unit` | string, required | the openams unit name, e.g. `"oams1"` |
| `bay` | integer, required | 0-based bay index in that unit |
| `material` | string | 1 to 32 characters |
| `color_hex` | string | exactly 6 hex digits, no `#`, any case; stored lowercase |
| `vendor` | string | 1 to 64 characters |
| `remaining_weight` | number | grams, 0 or more |
| `initial_weight` | number | grams, more than 0 |
| `flow_k` | number | pressure advance, 0 to 2; stored in the spool's `extra.flow_k`, encoded as §5.1 and §7.2 read it |

At least one of the six optional fields is required. Other keys are ignored.

**Result:** `{"spool_id": <int>, "created": <bool>, "bay": <this bay's entry>}`,
where `bay` is exactly the entry `bays.<unit>-<bay>` of the status object (§8)
after the change.

**Bay is linked** (the host's `oams_manager` status has a `spool_id` for it):
- `remaining_weight`, `initial_weight` and `flow_k` PATCH the spool (§5.1).
- `material`, `color_hex` and `vendor` **never modify a filament**, since other
  spools may share it. The component finds or creates the filament with the
  resulting vendor, material and color (vendor by §5.3, filament as §5.4 but by
  those three fields), then PATCHes the spool's `filament_id`. A new filament
  carries the current filament's diameter and weight; with the material
  unchanged it also carries the name, density and temperatures, and with a new
  material those come from the material defaults.
- `created` is `false`.

**Bay is not linked but holds a spool** (`ready`): the component creates the
vendor (default `"Generic"`), the filament (`material` is required; `color_hex`
defaults to none), and the spool (`initial_weight` default 1000 g,
`remaining_weight` default the initial weight), with `location` set to
`"<unit>-<bay>"` (§5.1), then links it:

```
OAMSM_SET_BAY_SPOOL OAMS=<unit> BAY=<bay> SPOOL=<id> SOURCE=manual
```

`created` is `true`. The link is shown in the result and the notification
at once; the host's own status then repeats it.

**Errors** are JSON-RPC errors whose message a person can read and that names the
field or bay:

| Case | Message (HTTP status) |
|---|---|
| a bad value or missing param | names the field, e.g. `flow_k must be between 0 and 2` (400) |
| unknown unit or bay | `unknown unit 'oamsX'`, `oams1 has no Spool 9` (404) |
| empty bay | `no spool in oams1 Spool 1` (409) |
| create without a material | `material is required to add a spool to oams1 Spool 1` (400) |
| Spoolman unreachable | `Spoolman is offline` (503); nothing is queued |
| Spoolman refuses | `Spoolman refused the change: <its message>` (502) |

**No half edit.** Validation and every read come first. A linked bay changes in
one spool PATCH, so it applies whole or not at all; the only earlier writes are a
vendor or filament that nothing uses yet, which a retry reuses. Weights go out
as `initial_weight` plus `used_weight`, so Spoolman cannot apply them in a
different order; `remaining_weight` may not exceed the initial weight, and an
`initial_weight` alone keeps the remaining weight. An unlinked bay creates the
spool in one request, then links it; if the host refuses the link the new spool is
deleted again. A connection lost between those two steps is the one case where
a spool with a `location` and no link can remain.

**After success** the component refreshes that bay's summary, ends
`pending_confirmation` when `remaining_weight` was given (the weight is now
known), and sends `notify_openams_spoolman_status`, so every UI updates at once.

### 9.1 Stopping the running sequence from a UI

A display's one Stop button stops the **whole** running sequence (home, heat,
cut, retract, feed and the user's hooks), not only the firmware load or
unload. G-code cannot do that, because a command queues behind the macro it
is meant to interrupt, so the component forwards to Klipper's `openams/stop`
webhook (C3 §4), which runs in the reactor while a macro waits. The status
object advertises the route with `"stop": true` (§8).

**JSON-RPC** `server.openams.stop` (HTTP `POST /server/openams/stop`).

| Param | Type | Rule |
|---|---|---|
| `lane` | string, optional | the FPS lane to stop; without it every lane that is running a sequence is stopped |

**Result** (Klipper's answer, unchanged): `{"result": "stopping"}` when at
least one lane was running a sequence, else `{"result": "nothing running"}`
(also for an unknown lane). It never errors for "nothing to stop", so a UI may
send it unconditionally. The step that was running ends failed and the lane's
`message` is `Stopped by the user` (C2 §5.3).

## 10. Open questions

Every item the design review resolved is marked **RESOLVED** inline
above, next to the field or behavior it settles, rather than repeated here.
What remains open:

1. **Removal of an untagged manual link** (§3): kept while the lane is in a
   runout or the bay is loaded, loading or unloading; cleared on the first empty
   read afterwards (the empty spool's identity is not retained). **RESOLVED
   (F-4); see §3.**
2. **Usage split at a runout reload** (§7.2): the error is within H4's "usage
   splits correctly" acceptance; the bound is recorded in §7.2. **RESOLVED
   (F-4).**
3. **Links made in the Spoolman web UI** (a spool's `location` set to a bay,
   gap analysis step 3): **not in v1** (C2 17); it needs a Spoolman watcher,
   since Moonraker's `[spoolman]` relays only deletions of the active spool
   (`moonraker:201-209`), not arbitrary spool edits. Listed as future work.
