# SUMEL – remote for Platin SM450

*Made out of frustration with poor or non-existent apps. =)*

SUMEL is an **unofficial** remote control for speakers built on the **Platin SM450** amplifier module (tested on the **Lemus HOME 1500**). It runs locally on your PC and is used from the browser on the PC or a phone on the same network. No cloud, no account.

> **Disclaimer:** SUMEL is not made by, affiliated with or endorsed by Lemus or Platin. It writes EQ settings to the speaker's DSP. Use at your own risk. Tested only on Lemus HOME 1500 (SM450, firmware MCU 177 / LS9 15262).

A full README with screenshots, features, installation and the protocol description is on its way.

## Quick start (from source)

Requires Python 3.10 or newer.

```
python -m lemus_remote --open
python -m lemus_remote --host 192.168.0.6 --open   # if the speaker is not found automatically
```

## License

MIT – see [LICENSE](LICENSE).
