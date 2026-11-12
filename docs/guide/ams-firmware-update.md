# Update the AMS firmware

The Cortex Bridge can install Bambu Lab's own firmware on your AMS units, the way a Bambu Lab printer does: a newer version, or an older one. It works for the AMS 1, the AMS 2 Pro and the AMS HT. Each update takes about 10 seconds, then up to a minute while the AMS restarts.

> [!IMPORTANT]
> **OpenAMS does not host Bambu Lab firmware.** OpenAMS does not host, store or distribute any Bambu Lab firmware, and none comes with it. You download Bambu Lab's own offline firmware package from bambulab.com yourself; for a few AMS 1 versions, OpenAMS downloads the image straight from Bambu Lab's own server. OpenAMS only checks the image and sends it to your AMS, the way a Bambu Lab printer would.

Your AMS stays on Bambu Lab firmware: only Bambu Lab's own signed firmware can be installed.

This is optional. The [Quick start](cortex-bridge-setup.md) doesn't ask you to change the AMS firmware.

Except where a step says otherwise, run the commands on the printer's Klipper computer, over SSH.

## 1. Update the bridge first

AMS updates need the bridge firmware that comes with this OpenAMS version. Update OpenAMS from Mainsail's **Machine → Update Manager**, entry `openams`. Then, with the printer idle and no filament loaded, install the bridge firmware it brings:

```bash
python3 ~/openams/tools/cortex-bridge.py update
```

Check which version the bridge runs:

```bash
python3 ~/openams/tools/cortex-bridge.py version
```

**Check:** the last line says `Up to date.` [Update the Cortex Bridge firmware](cortex-bridge-update.md) explains both commands.

If Mainsail still asks for FIRMWARE_RESTART after the bridge update, press **FIRMWARE RESTART** in Mainsail. That's expected and harmless.

If the bridge firmware is too old, the AMS update in step 5 stops before it starts and tells you to run `cortex-bridge.py update`.

## 2. See what your AMS runs

```bash
python3 ~/openams/tools/cortex-bridge.py ams-firmware list
```

```text
Connected AMS units:
  3f9a1c07b2d4 AMS 1      00.00.06.40  01.00.07.00 is listed
  8e21d5a0c6f3 AMS 2 Pro  03.00.21.29  05.00.22.22 is listed
  c47b90e2a1d8 AMS HT     03.00.21.29  05.00.22.19 is listed

Listed AMS firmware (from Bambu Lab; * = on this computer):
  AMS 1      01.00.07.00  rev7*, rev8* P2S 01.03.00.00 offline package
  AMS 1      01.00.06.87  rev7, rev8   H2D 01.03.00.00 offline package
  ...
  AMS HT     03.00.21.29  rev5         P1 series 01.10.00.00 offline package
```

- **Connected AMS units:** each AMS on the bridge, with its id, its model, the firmware it runs, and the newest version listed for it.
- **Listed AMS firmware:** every version this OpenAMS knows, newest first, and where Bambu Lab publishes it. A `*` means the firmware is already on this computer.

Note the id of the AMS you want to update, such as `8e21d5a0c6f3`.

## 3. Get the firmware from Bambu Lab

OpenAMS does not host any Bambu Lab firmware. This computer gets it from Bambu Lab, from Bambu Lab's own server or from the package you download from bambulab.com, and checks it before it is used.

### Versions that download by themselves

A few AMS 1 versions download straight from Bambu Lab's own server, not from OpenAMS: in the list, their line doesn't say `offline package`. The update in step 5 downloads them itself. To download one ahead of time:

```bash
python3 ~/openams/tools/cortex-bridge.py ams-firmware fetch --family ams1 --version 00.00.06.49
```

### All other versions: Bambu Lab's offline package

Every other version comes inside the offline update package of a Bambu Lab printer. You download that package from bambulab.com yourself; OpenAMS does not host it.

1. On your PC, go to bambulab.com > **Support** > **Firmware download** and choose the printer whose package carries the version you want (see the table below). Download its offline update package, a .zip file of more than 1 GB.
2. From your PC, copy the .zip to the printer's Klipper computer, for example with `scp` (use your own user name and printer address):

   ```bash
   scp offline-ota-p2s_v01.03.00.00-20260921090417.zip <user>@<printer-address>:~/
   ```

3. On the Klipper computer, import it:

   ```bash
   python3 ~/openams/tools/cortex-bridge.py ams-firmware import ~/offline-ota-p2s_v01.03.00.00-20260921090417.zip
   ```

   ```text
   AMS 1 01.00.07.00 (rev7): checked, 95928 bytes, /home/pi/.cache/openams/ams-firmware/...
   AMS 1 01.00.07.00 (rev8): checked, 97380 bytes, /home/pi/.cache/openams/ams-firmware/...
   AMS 2 Pro 05.00.22.22 (rev5): checked, 172132 bytes, /home/pi/.cache/openams/ams-firmware/...
   AMS HT 05.00.22.19 (rev5): checked, 160552 bytes, /home/pi/.cache/openams/ams-firmware/...
   ```

   The command only reads the AMS firmware files this OpenAMS lists out of the package you downloaded, checks each one, and keeps a copy on this computer. Nothing is uploaded anywhere, and nothing changes on your AMS.

4. You can delete the .zip afterwards.

Which package carries which version:

| Bambu Lab offline package | AMS HT | AMS 2 Pro | AMS 1 |
|---|---|---|---|
| P2S 01.03.00.00 (the newest versions) | 05.00.22.19 | 05.00.22.22 | 01.00.07.00 |
| H2D 01.03.00.00 | 04.00.21.86 | 04.00.21.87 | 01.00.06.87 |
| P1 series 01.10.00.00 | 03.00.21.29 | 03.00.21.29 | 01.00.06.83 |

`... holds no AMS firmware this OpenAMS lists` means the package is another printer firmware version than the ones in this table.

## 4. Before you update

- No print is running or paused.
- No dryer is running, and no AMS is loading or unloading.
- The toolhead is unloaded.
- The bridge's USB cable and 24 V stay connected, and the AMS stays powered.
- You can leave the terminal open until the update says `Done`. Closing it stops the update.

If a print or a dryer is running, the update stops before it changes anything.

## 5. Update the AMS

Give the id from step 2. Without `--version`, the newest listed version is installed:

```bash
python3 ~/openams/tools/cortex-bridge.py ams-firmware update --unit 8e21d5a0c6f3
```

To install another version, add it:

```bash
python3 ~/openams/tools/cortex-bridge.py ams-firmware update --unit 8e21d5a0c6f3 --version 04.00.21.87
```

The command says what will change and asks before it starts. Answer `y`:

```text
AMS 2 Pro 8e21d5a0c6f3 (index 1): 03.00.21.29 -> 05.00.22.22 (upgrade)
The AMS stops answering for a minute or two while it updates; Klipper stays connected. Do not power the AMS or the bridge off until this says Done.
Update it now? [y/N] y
  Quieting the bus and checking the image
  Asking the AMS to enter its loader
  Checking that only this AMS entered its loader (loader: N3F05)
  Sending the firmware
    block 17 of 168
    ...
    block 168 of 168
  The AMS is checking the firmware
  The AMS is restarting
  Waiting for the AMS to answer again
  Done
  After the update: index 0 application, index 1 application, index 128 application
Done: AMS 2 Pro 8e21d5a0c6f3 now runs 05.00.22.22.
Klipper restarted (FIRMWARE_RESTART) to read what the new firmware can do.
```

> [!WARNING]
> **Don't power off the AMS or the bridge, and don't close the terminal, until the command says `Done`.**

- Klipper stays connected the whole time. Your other AMS units pause during the update and answer again within seconds after it.
- For an AMS 1, the command first reads which board the AMS has and picks the matching firmware, so you don't give the board. It says so: `The AMS reports board AMS08.`
- The `After the update` line shows every AMS on the bus. `application` means it runs its normal firmware.
- At the end the command restarts Klipper (FIRMWARE_RESTART), because what Klipper knows about an AMS can change with its firmware version. If Mainsail still asks for FIRMWARE_RESTART afterwards, press **FIRMWARE RESTART** in Mainsail. That's expected and harmless.

**Check:** the `Done:` line names the new version, as the AMS itself reports it. `ams-firmware list` shows it too.

## 6. Go back to an older version

A downgrade is the same command with an older version. Bambu Lab's loader accepts older firmware. Get that version first (step 3), then:

```bash
python3 ~/openams/tools/cortex-bridge.py ams-firmware update --unit 8e21d5a0c6f3 --version 03.00.21.29
```

The first line ends in `(downgrade)`.

## What keeps your AMS safe

- **Every file is checked** before it is sent: its size, its MD5 checksum, its header, and the board name written inside it. Firmware for another AMS model or board is never sent.
- **Only Bambu Lab's signed firmware can be installed.** The AMS checks Bambu Lab's signature itself and refuses anything else.
- **Only the AMS you chose is touched.** Before anything is erased, the bridge checks that only that AMS entered its loader and that every other AMS still answers. If not, it stops with nothing erased.

## If an update is interrupted

If the power, the USB cable or the terminal was lost during an update, the AMS waits in its loader for its firmware. It shows offline, and `ams-firmware list` doesn't show it. Running the update again resumes it, without a power cycle, even if the bridge restarted meanwhile.

1. Check that it waits:

   ```bash
   python3 ~/openams/tools/cortex-bridge.py ams-firmware listen
   ```

   For 15 seconds it shows every line an AMS loader sends. An AMS waiting in its loader sends one about every second, such as `wait cmd1!`, and the command ends with `an AMS waits in its loader; run the update for it again.`

2. Run the same update again. The waiting AMS isn't in the list, so name it with `--resume`, its bus index and its model:

   ```bash
   python3 ~/openams/tools/cortex-bridge.py ams-firmware update --resume --unit 1 --family ams2 --version 05.00.22.22
   ```

   - `--unit`: the AMS's bus index, shown on the first line of the interrupted update, such as `(index 1)`. It is 0 to 3 for an AMS 1 or AMS 2 Pro, and 128 or higher for an AMS HT.
   - `--family`: `ams1` for an AMS 1, `ams2` for an AMS 2 Pro, `ams_ht` for an AMS HT.
   - `--version`: the version you were installing. Without it, the newest listed version is installed.
   - If the command asks for the board of an AMS 1, add `--board rev8` when the interrupted update said `The AMS reports board AMS08`, or `--board rev7` for `AMS07`.

Only if an AMS stays offline for more than two minutes after `Done` should you power it off and on (an AMS HT or AMS 2 Pro also needs its power adapter unplugged). The command says so in that case.

## Moving an AMS back to a Bambu Lab printer

We tested this with a P1S on firmware 01.10.00.00 and an AMS HT on 05.00.22.19:

- As soon as the AMS is plugged in, the printer asks to update its firmware. That update is a downgrade to the version the printer carries (03.00.21.29 here).
- **Accept the printer's update.** The AMS then works normally with the printer again (drying or printing, not both).
- If you decline, the AMS HT offers no drying, and it loads filament but cannot unload it, so the printer is stuck.

Or downgrade the AMS with OpenAMS before you move it (step 6), to the version that printer's firmware carries. For a P1S on firmware 01.10.00.00, that's 03.00.21.29 for an AMS HT or AMS 2 Pro, and 01.00.06.83 for an AMS 1. All three are in Bambu Lab's P1 series 01.10.00.00 package.

## If something goes wrong

| The command says | What to do |
|---|---|
| `Klipper is printing: ...` or `Klipper is paused: ...` | Let the print finish, or cancel it, then run the update again. |
| `the bridge is busy: an AMS is loading, unloading, drying or moving, or another update runs` | Wait until every AMS is idle, then run the update again. |
| `bridge firmware ... cannot update an AMS` or `... cannot resume an AMS waiting in its loader` | Update the bridge first (step 1). |
| `... comes in Bambu Lab's offline package ..., not as a download` | Import that package first (step 3). |
| `... is not listed; listed: ...` | Use one of the versions it names. |
| `the AMS did not enter its loader (no answer for 15 s); its firmware is unchanged` | Nothing changed. Run the update again. |
| `another AMS stopped answering or entered its loader too, so the bridge stopped before erasing anything` | Nothing changed. Check the cables and power of the AMS it names, then run the update again. |
| `... already runs ...` | Nothing to do: the AMS already runs that version. |

To send support everything they need, run `python3 ~/openams/tools/cortex-bridge.py support`.
