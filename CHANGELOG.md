# Changelog

All notable changes to SUMEL, newest first. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Downloads are on the [releases page](https://github.com/erognmo-afk/SUMEL-platin-sm450-remote/releases).

## [1.0.1] - 2026-11-09

### Changed

- **Input selection** waits for the speaker's own source list to confirm the new input (up to 6 s) and always shows the input the speaker reports, never the one that was requested. Selecting an input that is already shown as active is sent again, so a stale display can no longer block switching back to HDMI.
- If the speaker does not answer at all, SUMEL reconnects straight away and says so, instead of showing an old input until the connection watchdog notices.
- If the speaker's source list marks more than one input as active, the input is shown as unknown rather than as HDMI.
- The lower row (Sound and Profile) now looks like part of the amplifier: walnut sides, engraved labels and buttons in a recessed well, aligned with the front panel.
- Room correction and Custom EQ in the lower row show only a lamp and the name (no "On/Off" text or explanation).
- On a PC, the previous/play/next buttons match the input selector and sit on the same line.
- The input buttons have the same width.

### Known issue

- After switching to HDMI over the network, the SM450 can report HDMI as active while no sound comes out. This also happens with the Platin app, so it is the amplifier's firmware, not SUMEL. The RF remote fixes it: switch to AUX/OPT and back to HDMI with the remote.

## [1.0.0] - 2026-11-09

### Added

- A small speaker symbol in the middle of the volume knob shows that tapping there mutes. It lights amber with a cross while the sound is muted. Until you have muted once on a device, the line under the knob says how to mute (**Click the centre of the knob to mute** on a PC, **Tap the knob to mute** on a phone).
- Advice for a filter outside SUMEL's three EQ slots: switch the speaker fully off (power switch or wall socket) for 10 seconds. That probably loads the factory setup again (not confirmed).
- Room tab: the tag says **No measurement** when the speaker holds no room measurement.

### Changed

- **EQ only in the three app slots.** SUMEL writes only to the three EQ slots the SM450 reserves for app EQ. Filters in other slots (the manufacturer's tuning) are shown as locked and are never touched: not changed, not switched off or on, and not deleted. They take none of the three slots.
- **Profiles** keep a high-pass filter and ask before replacing your own filters, as in 0.10.1. A profile that does not fit next to the filters it keeps is refused straight away, with a message saying how many slots are free.
- **Curve** and the **tone knobs** say so when the EQ slots are taken by filters SUMEL can't change. Curve then keeps **Apply** switched off.
- Until the speaker's EQ has been read (for example while it is in standby), the display says **EQ not read yet** and no profile is shown as active.
- The backup file only holds the filters SUMEL can write.
- **New look** (visual only, nothing works differently):
  - The lamps on the aluminium strip sit in a small, dark indicator window.
  - Corners follow one small, consistent radius.
  - Capitals are used only on the front panel.
  - Amber is used only for sound and status.
  - The volume status is calmer: "Sending …" only shows when the speaker is slow to answer, and "Confirmed" no longer flashes after each change.
  - On a phone, the speaker info is shown in one column.
- After waking from standby, the speaker goes back to the input it had, also when it reports power before it reports the input.
- Right after a wake, SUMEL waits up to 10 seconds for the DSP to answer before it treats a silent EQ read as "no filters". A filter it has not seen is never overwritten.
- EQ changes are handled one at a time, so two devices adding a point at the same moment get different slots.

### Security

- The volume guard (largest jump in one command, 2–25 steps, set under **More → Volume safety**) always applies and can't be bypassed from the browser. A value outside 2–25 in the settings file is limited when SUMEL starts.
- Filters SUMEL can't write (locked or unknown types) can't be overwritten through SUMEL's web API either.
- Stricter checks on requests to SUMEL's web server. A malformed request gets a clear error message.

## [0.10.1] - 2026-11-09

Safer first start with a new speaker.

### Added

- If SUMEL is still searching for the speaker after about 20 seconds, an **Enter IP address** button and help appear.
- If the speaker is found but does not report whether it is on, SUMEL says so after about 15 seconds.
- README: **Getting started**, and **Første gang** in the Norwegian part.

### Changed

- Profiles keep a high-pass filter and other filters they don't use. They ask before replacing filters that don't come from a profile, with the question at the bottom of the screen.
- If the speaker is in standby when SUMEL starts, the EQ is shown as unknown, not as empty. EQ, room correction and the room curve are read again when the speaker wakes. EQ changes are blocked until the filters have been read.
- Room tab: says **No measurement found** when the speaker holds no measurement, and says plainly if the measurement has an unknown format.
- Neutral texts: "Platin SM450" instead of "Lemus HOME 1500" until the speaker's name is known, and "Tested on Lemus HOME 1500" instead of "Confirmed".
- README: SUMEL does not check the speaker model yet and does not ask before using untested features. On another model, try volume and input first.

## [0.10.0] - 2026-11-09

First public release.

### Added

- **Volume** with a volume guard: one command moves at most 25 steps (adjustable 2–25 under **More**), and volume is never sent when the app opens.
- **Input** (HDMI, AUX/OPT, Bluetooth, Streaming) and **play/pause/next/previous** for streaming sources.
- **Power**: standby, and waking from standby by playing 3 seconds of silence over DLNA, then back to the previous input.
- **Mute** by tapping the volume knob or pressing **M**. SUMEL uses the speaker's own mute, and falls back to volume 0 if the speaker does not confirm.
- **Room correction** on/off, and the room curve stored in the speaker (measured and corrected).
- **EQ:**
  - **Curve** (first and default): drag 10 points, and SUMEL picks the at most three filters that come closest. Nothing is sent until **Apply**.
  - **Points:** edit the three filters directly.
  - **Tone knobs** (bass, mid, treble) on the front panel.
  - **Custom EQ** on/off for a quick A/B against no EQ.
- **Profiles:** built-in Neutral, Warm, Music, TV / dialogue and Evening (editable, with reset), your own profiles, A/B between two profiles, undo, and backup/restore as a JSON file. Profiles are stored on the PC, never in the speaker.
- **Live status** when the speaker's remote or the Platin app changes something.
- **Web app** for PC and phone, also as a home-screen app. Other devices than the PC enter an access code once, or scan the QR code under **More**.
- **Windows installer** (`SUMEL-Setup-0.10.0.exe`): an icon next to the clock with the phone address, the access code, autostart and Quit. Optional start with Windows, and a firewall rule for private networks only. The installer is not code-signed: if SmartScreen warns, choose **More info → Run anyway**. Smart App Control blocks it completely; then run SUMEL from source.
- **Run from source** with Python 3.10 or newer, standard library only.
- **English** by default, **Norwegian** when the browser (or Windows) is set to Norwegian. Switch under **More → Språk / Language**.
- Front panel with or without the aluminium strip (**More → Appearance**), with the strip by default.
- An About line under **More**: *Made out of frustration with poor or non-existent apps. =)*
- [PROTOCOL.md](PROTOCOL.md) with the protocol notes, and the MIT license.

### Changed

- New name: **SUMEL** ("LEMUS" spelled backwards), earlier **Lemus Remote**. New icons. The settings folder (`%APPDATA%\LemusRemote`) and the Python package (`lemus_remote`) keep the old name.

### Security

- At most three EQ filters. During testing, a fourth filter caused a loud pop. The SM450 datasheet reserves only three filters for app EQ.
- A closed list of known commands. Factory reset, firmware updates and crossovers can't be reached.
- Every setting change waits until the speaker reports the new value back.
- Finding the speaker sends no control commands.

## 0.1–0.9 - 2026-11-08

Development under the name Lemus Remote, before the public release. Not released publicly.

- **0.1.0:** a local web remote for the SM450, with volume, input, playback, room correction on/off and live status from the speaker.
- Speaker presets 1–3 were left out: on the SM450 they fall back to the app EQ after a few seconds.
- EQ filters: create, change and delete, also by dragging points in the curve. Every write is read back.
- Automatic search for the speaker, with the firmware versions shown.
- Power on from standby with a short silent clip over DLNA.
- Profiles, undo, an access code for other devices than the PC, and a volume guard against big jumps.
- A hard limit of three EQ filters, after a fourth filter caused a loud pop during testing.
- English and Norwegian, and a home-screen app on phones.
- **0.9.0:** Curve EQ, the stored room curve, wider tone controls, and a Windows program with a tray icon and installer.

[1.0.1]: https://github.com/erognmo-afk/SUMEL-platin-sm450-remote/releases/tag/v1.0.1
[1.0.0]: https://github.com/erognmo-afk/SUMEL-platin-sm450-remote/releases/tag/v1.0.0
[0.10.1]: https://github.com/erognmo-afk/SUMEL-platin-sm450-remote/releases/tag/v0.10.1
[0.10.0]: https://github.com/erognmo-afk/SUMEL-platin-sm450-remote/releases/tag/v0.10.0
