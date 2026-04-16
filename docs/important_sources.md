# Important Sources

## Architecture

There is a distinction between the **commercial** and **community** versions of the Carvera firmware and controller. We are currently running the commercial version — this should be checked at startup.

- **Firmware**: Runs on the machine itself
- **Controller**: Runs on the PC, communicates with the machine over serial or WiFi

---

## OctoPrint

- OctoPrint core: https://github.com/octoprint
- Better Grbl Support plugin: https://github.com/synman/Octoprint-Bettergrblsupport

---

## Carvera Firmware

Both the Makera (commercial) and Community firmwares are based on **Smoothieware 1.x**.

| Variant | Repository |
|---------|-----------|
| Smoothieware (upstream) | https://github.com/Smoothieware/Smoothieware |
| Smoothieware (brooklikeme fork) | https://github.com/brooklikeme/Smoothieware |
| Commercial firmware | https://github.com/MakeraInc/CarveraFirmware |
| Community firmware | https://github.com/Carvera-Community/Carvera_Community_Firmware |

---

## Carvera Controller

| Variant | Repository |
|---------|-----------|
| Commercial controller | https://github.com/MakeraInc/CarveraController |

---

## Command Reference

The community docs have supported commands listed, though incomplete (e.g. `?` realtime query is not documented):

- M codes: https://carvera-community.gitbook.io/docs/firmware/supported-commands/mcodes
- G codes: https://carvera-community.gitbook.io/docs/firmware/supported-commands/gcodes
- Console commands: https://carvera-community.gitbook.io/docs/firmware/supported-commands/console-commands

At minimum, any new commands we add or discover should be documented in `docs/carvera-serial-protocol.md`.
