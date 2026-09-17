# PowerBar

A [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin that puts your
Mac's power draw, battery and thermals in the menu bar — for effectively
nothing, so the monitor isn't a meaningful part of what it measures.

Apple silicon only. No root, no sudoers, no kernel extension.

## What it shows

- **Menu bar** — live system draw in watts on mains (`⚡ 24 W`), battery
  percentage on battery.
- **Power** — system and package watts, with the package split across CPU,
  GPU, Neural Engine and RAM. Utilisation and clock speed per cluster
  (efficiency and performance cores separately), GPU frequency, RAM and swap
  use, and a stacked bar showing where the package power is going.
- **Battery** — charge and state, watts into or out of the pack, time to
  full or empty, health against design capacity, cycle count, cell
  temperature, voltage and current, and the connected charger's name and
  wattage.
- **Thermals** — CPU, GPU and battery temperatures, fan speeds, and a warning
  if macOS is throttling the scheduler.
- **Top processes** by CPU, and a shortcut to Activity Monitor.
- **Graphs** — the last hour of system power, battery charge and CPU
  temperature, with min / avg / max. History persists across restarts.
- **Its own cost** — the last row shows how much CPU the plugin itself used
  for the current sample.

## How it's kept cheap

Most menu bar monitors are a script that SwiftBar re-runs every few seconds.
That is fine for a runner status, but power sampling has a fixed setup cost:
`macmon` spends about 0.2 CPU-seconds enumerating IOReport channels and
sensors every time it starts, and the first version of this plugin — a shell
script spawning macmon, ioreg, ps and a handful of python processes per
refresh — cost about 0.6 CPU-seconds every 10 seconds, roughly 6% of a core.

PowerBar is instead a SwiftBar *streaming* plugin: one Python process that
SwiftBar starts once and that stays resident. It holds a single `macmon pipe`
open for its lifetime, so the setup cost is paid once, and each sample is one
line read from that pipe, one `ioreg` dump, one `ps` listing and three small
PNGs drawn in-process with the standard library (no ImageMagick, no pip
packages). Measured on an M4 Pro:

| | CPU per 10 s sample |
|---|---|
| PowerBar process + ioreg + ps | ~30–60 ms (0.3–0.6% of one core) |
| macmon, steady state | not measurable above zero |
| macmon, one-off at launch | ~0.2 s |

At the low clocks an efficiency core idles at, that is on the order of
milliwatts against a system that draws tens of watts. The sampling interval
is `SAMPLE_S` at the top of the script; 30 s cuts the cost by a further
two-thirds if you want it.

## Requirements

- macOS on Apple silicon with [SwiftBar](https://github.com/swiftbar/SwiftBar)
  (`brew install --cask swiftbar`)
- [`macmon`](https://github.com/vladkens/macmon) (`brew install macmon`)
  for package power, temperatures and fans — it reads IOReport without root.
  Without it the plugin still runs and shows battery data only.
- `python3` — the one Xcode Command Line Tools or Homebrew provides.

## Install

```sh
git clone https://github.com/XDGFX/powerbar.git
cd powerbar && bash install.sh
```

The script installs SwiftBar and macmon if they're missing, wires the plugin
in (pointing SwiftBar at the clone, so `git pull` updates it in place), and
sets SwiftBar to start at login. There is no configuration.

## Where the numbers come from

- Battery and charger: IOKit's `AppleSmartBattery` service via `ioreg`.
  Health is raw full-charge capacity over design capacity, which is what
  System Settings shows too. Watts into or out of the pack are pack voltage
  times instantaneous current.
- Package power, per-block power, clocks, utilisation, temperatures and
  fans: `macmon`, which reads the same IOReport energy counters
  `powermetrics` does, minus the root requirement. "System" power is the
  whole machine including display and peripherals; "package" is the SoC.
- Per-process figures are CPU share from `ps`. Per-process *energy* impact
  (what Activity Monitor's Energy tab shows) is only available to root via
  `powermetrics --samplers tasks`, so it's not here.
- Throttling: `pmset -g therm`'s CPU scheduler limit.

## Notes

- SwiftBar starts a streaming plugin once and does not restart it on
  *Refresh all*. After editing the script, disable and re-enable the plugin
  in SwiftBar's preferences, or restart SwiftBar.
- SwiftBar's own footer rows (About, Run in Terminal, and friends) are hidden
  via the plugin's metadata; to also hide SwiftBar's app icon from the menu
  bar, untick it in SwiftBar's preferences.

## Licence

MIT.
