#!/usr/bin/env -S PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin python3
# PowerBar — a SwiftBar plugin for Mac power, battery and thermals.
#
# Menu bar: live system draw in watts on mains, battery percent on battery.
# Dropdown: package power split across CPU, GPU, ANE and RAM with utilisation
# and clocks; battery charge, health, cycles, cell temperature, time
# remaining and the charger; CPU/GPU temperatures and fans; the processes
# burning the most CPU; rolling graphs of power, charge and temperature.
#
# Built to cost effectively nothing. It is a *streaming* plugin: SwiftBar
# starts it once and it stays resident, sleeping between samples, instead of
# being re-spawned every refresh. A single `macmon` process is kept open for
# the lifetime of the plugin (its startup — enumerating IOReport channels and
# sensors — is the expensive part; each subsequent sample is nearly free).
# Every SAMPLE_S seconds it reads one macmon line, one ioreg dump and one ps
# listing, draws three small PNGs in-process, and prints a new menu.
#
# Requirements: macOS on Apple silicon, python3 (Xcode CLT or Homebrew),
# `brew install macmon`. Without macmon it degrades to battery-only data.
#
# <swiftbar.type>streamable</swiftbar.type>
# <swiftbar.useTrailingStreamSeparator>true</swiftbar.useTrailingStreamSeparator>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

import base64, json, os, re, shutil, signal, struct, subprocess, sys, time, zlib

SAMPLE_S = 10                       # seconds between samples
KEEP = 360                          # samples kept: 1 h at 10 s
STATE = os.path.expanduser("~/.local/state/powerbar")
HISTORY = os.path.join(STATE, "history.tsv")
os.makedirs(STATE, exist_ok=True)

# Colours: "light,dark". Giving every row an explicit colour also stops
# SwiftBar rendering action-less rows in the disabled grey.
HEAD = "color=#1d1d1f,#f5f5f7"
BODY = "color=#3a3a3c,#e5e5ea"
DIM = "color=#8e8e93,#8e8e93"
MONO = "font=Menlo size=11"
BLUE, GREEN, AMBER, RED = "0a84ff", "30d158", "ffd60a", "ff453a"
ORANGE, PURPLE, TEAL, GREY = "ff9f0a", "bf5af2", "64d2ff", "636366"

# Rows that have somewhere relevant to go. Headings carry their symbol inline
# (:name:) rather than as sfimage=, because SwiftBar tints an sfimage with the
# row's light-mode colour and near-black vanishes on a dark menu.
BATTERY_SETTINGS = "bash=/usr/bin/open param1=x-apple.systempreferences:com.apple.Battery-Settings-extension terminal=false"
ACTIVITY_MONITOR = "bash=/usr/bin/open param1=-a param2='Activity Monitor' terminal=false"

# --- PNG drawing (pure stdlib) ----------------------------------------------
# Everything is drawn at 2x and told to SwiftBar at 1x (width=/height=) so it
# is crisp on Retina.

def png(w, h, rows):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    ).decode()

def rgb(hexc):
    return bytes((int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)))

def area_chart(vals, W, H, hexc, lo=None, hi=None):
    """Filled area chart, right-aligned so the latest sample is at the edge."""
    S = 2
    w, h = W * S, H * S
    c = rgb(hexc)
    line, fill, clear = c + b"\xeb", c + b"\x50", b"\x00\x00\x00\x00"
    if not vals:
        vals = [0.0]
    vmin, vmax = min(vals), max(vals)
    if lo is None:
        lo = max(0.0, vmin - 0.25 * (vmax - vmin or 1))
    if hi is None:
        hi = vmax + 0.1 * (vmax - vmin or 1)
    span = max(hi - lo, 1e-6)
    # the canvas is one full history window (KEEP samples) wide, latest at the
    # right edge; a shorter history leaves the left empty rather than stretching
    n = len(vals)
    scaled = [int(round((min(max(v, lo), hi) - lo) / span * (h - 1))) for v in vals]
    heights = []
    for x in range(w):
        i = x * KEEP // w - (KEEP - n)
        heights.append(scaled[i] if i >= 0 else -1)
    rows = []
    for y in range(h):
        depth = h - 1 - y                # 0 at the bottom
        row = bytearray()
        for ht in heights:
            if ht < 0 or depth > ht:
                row += clear
            elif depth >= ht - 1:
                row += line
            else:
                row += fill
        rows.append(bytes(row))
    return f"| image={png(w, h, rows)} width={W} height={H}"

def stacked_bar(parts, W, H):
    """Horizontal stacked bar. parts: [(fraction, hex)], fractions sum ≤ 1."""
    S = 2
    w, h = W * S, H * S
    row = bytearray()
    for frac, hexc in parts:
        row += (rgb(hexc) + b"\xff") * int(round(frac * w))
    row += (rgb(GREY) + b"\x60") * (w - len(row) // 4)
    row = bytes(row[: w * 4])
    return f"| image={png(w, h, [row] * h)} width={W} height={H}"

# --- Data sources -------------------------------------------------------------

def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout

def battery():
    out = sh("ioreg", "-rn", "AppleSmartBattery")
    def f(k, default=0):
        m = re.search(r'^\s*"%s" = (\S+)' % k, out, re.M)
        return m.group(1) if m else default
    def n(k):
        try:
            return int(f(k))
        except ValueError:
            return 0
    ma = n("InstantAmperage")
    if ma > 2**32:                   # unsigned wrap of a negative (discharge) current
        ma -= 2**64
    b = dict(
        pct=n("CurrentCapacity"), raw_max=n("AppleRawMaxCapacity"), design=n("DesignCapacity"),
        cycles=n("CycleCount"), cell_c=n("Temperature") / 100, mv=n("Voltage"), ma=ma,
        charging=f("IsCharging") == "Yes", external=f("ExternalConnected") == "Yes",
        full=f("FullyCharged") == "Yes", mins=n("TimeRemaining"),
    )
    b["watts"] = b["mv"] * ma / 1e6
    b["health"] = round(100 * b["raw_max"] / max(b["design"], 1))
    m = re.search(r'"AdapterDetails" = \{.*?"Watts"=(\d+)', out)
    b["adapter_w"] = int(m.group(1)) if m else 0
    m = re.search(r'"AdapterDetails" = \{.*?"Name"="([^"]*)"', out)
    b["adapter"] = m.group(1).strip() if m else ""
    return b

def top_processes(n=6):
    rows = []
    for line in sh("ps", "-Aro", "%cpu=,comm=").splitlines()[:n]:
        cpu, _, cmd = line.strip().partition(" ")
        rows.append((float(cpu), os.path.basename(cmd.strip())))
    return rows

def thermal_limit():
    m = re.search(r"CPU_Scheduler_Limit\s*=\s*(\d+)", sh("pmset", "-g", "therm"))
    return int(m.group(1)) if m else 100

class Macmon:
    """One long-lived `macmon pipe`; read() returns the latest sample dict."""
    def __init__(self):
        self.proc = None
        if shutil.which("macmon"):
            self.proc = subprocess.Popen(
                ["macmon", "pipe", "-i", str(SAMPLE_S * 1000)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    def read(self):
        if not self.proc:
            return None
        line = self.proc.stdout.readline()
        if not line:                 # macmon died; give up on it for this run
            self.proc = None
            return None
        d = json.loads(line)
        t, m = d.get("temp", {}), d.get("memory", {})
        return dict(
            sys_w=d["sys_power"], pkg_w=d["all_power"], cpu_w=d["cpu_power"],
            gpu_w=d["gpu_power"], ane_w=d["ane_power"], ram_w=d["ram_power"],
            cpu_pct=100 * d["cpu_usage_pct"], e_pct=100 * d["ecpu_active_ratio"],
            p_pct=100 * d["pcpu_active_ratio"], e_mhz=d["ecpu_freq_mhz"],
            p_mhz=d["pcpu_freq_mhz"], gpu_pct=100 * d["gpu_active_ratio"],
            gpu_mhz=d["gpu_freq_mhz"], cpu_c=t.get("cpu_temp_avg", 0),
            gpu_c=t.get("gpu_temp_avg", 0),
            fans=[(f["rpm"], f["max_rpm"]) for f in d.get("fans") or []],
            ram_pct=100 * m.get("ram_usage", 0) / max(m.get("ram_total", 1), 1),
            swap_gb=m.get("swap_usage", 0) / 2**30,
        )
    def stop(self):
        if self.proc:
            self.proc.terminate()

# --- History ------------------------------------------------------------------

def load_history():
    try:
        with open(HISTORY) as fh:
            return [[float(x) for x in l.split("\t")] for l in fh if l.strip()][-KEEP:]
    except (OSError, ValueError):
        return []

def save_history(hist):
    tmp = HISTORY + ".tmp"
    with open(tmp, "w") as fh:
        fh.writelines("\t".join(f"{x:g}" for x in r) + "\n" for r in hist)
    os.replace(tmp, HISTORY)

def stats(col, hist):
    xs = [r[col] for r in hist]
    return f"min {min(xs):.1f} · avg {sum(xs)/len(xs):.1f} · max {max(xs):.1f}"

# --- Render -------------------------------------------------------------------

def eta(mins):
    return "—" if mins in (0, 65535) else f"{mins // 60}h {mins % 60:02d}m"

def render(mm, b, hist, procs, cost):
    o = []
    on_ac = b["external"]

    # menu bar
    if mm:
        o.append(f":bolt.fill: {mm['sys_w']:.0f} W | sfsize=12" if on_ac
                 else f":battery.50percent: {b['pct']}% | sfsize=12")
    else:
        o.append(f":battery.50percent: {b['pct']}% | sfsize=12")
    o.append("---")

    if mm:
        o.append(f":bolt.circle.fill: Power | {HEAD} size=13 sfsize=13 {ACTIVITY_MONITOR}")
        o.append(f"{mm['sys_w']:5.1f} W  system | {MONO} {BODY}")
        o.append(f"{mm['pkg_w']:5.1f} W  package | {MONO} {BODY}")
        pk = max(mm["pkg_w"], 1e-6)
        o.append(f"{mm['cpu_w']:5.1f} W  CPU  ·  {mm['cpu_pct']:.0f}%  (E {mm['e_pct']:.0f}% @ {mm['e_mhz']:.0f}  ·  P {mm['p_pct']:.0f}% @ {mm['p_mhz']:.0f} MHz) | {MONO} {DIM}")
        o.append(f"{mm['gpu_w']:5.1f} W  GPU  ·  {mm['gpu_pct']:.0f}% @ {mm['gpu_mhz']:.0f} MHz | {MONO} {DIM}")
        o.append(f"{mm['ane_w']:5.1f} W  ANE | {MONO} {DIM}")
        o.append(f"{mm['ram_w']:5.1f} W  RAM  ·  {mm['ram_pct']:.0f}% used  ·  swap {mm['swap_gb']:.1f} GB | {MONO} {DIM}")
        o.append(stacked_bar([(mm["cpu_w"] / pk, BLUE), (mm["gpu_w"] / pk, PURPLE),
                              (mm["ane_w"] / pk, TEAL), (mm["ram_w"] / pk, ORANGE)], 260, 6))
        o.append(f"CPU · GPU · ANE · RAM share of package | size=10 {DIM}")
        o.append(area_chart([r[1] for r in hist], 260, 40, BLUE))
        o.append(f"System power, last hour · {stats(1, hist)} W | size=10 {DIM}")
        o.append("---")

    if b["charging"]:
        state, icon = "Charging", "battery.100percent.bolt"
    elif b["full"]:
        state, icon = "Full", "battery.100percent"
    elif on_ac:
        state, icon = "On mains, not charging", "battery.75percent"
    else:
        state, icon = "Discharging", "battery.50percent"
    col = GREEN if on_ac else (RED if b["pct"] <= 20 else AMBER)
    o.append(f":{icon}: Battery {b['pct']}%  ·  {state} | {HEAD} size=13 sfsize=13 {BATTERY_SETTINGS}")
    if not on_ac:
        o.append(f"{-b['watts']:5.1f} W  from battery  ·  {eta(b['mins'])} remaining | {MONO} color=#{col}")
    elif b["charging"]:
        o.append(f"{b['watts']:5.1f} W  into battery  ·  full in {eta(b['mins'])} | {MONO} color=#{col}")
    o.append(f"Health {b['health']}%  ·  {b['raw_max']} / {b['design']} mAh design  ·  {b['cycles']} cycles | {MONO} {DIM} {BATTERY_SETTINGS}")
    o.append(f"Cell {b['cell_c']:.1f} °C  ·  {b['mv']/1000:.2f} V  ·  {b['ma']} mA | {MONO} {DIM}")
    if b["adapter"]:
        o.append(f"Adapter  {b['adapter']}  ·  {b['adapter_w']} W | {MONO} {DIM} sfimage=powerplug.fill")
    o.append(area_chart([r[4] for r in hist], 260, 24, col, lo=0, hi=100))
    o.append(f"Charge, last hour | size=10 {DIM}")
    o.append("---")

    if mm:
        o.append(f":thermometer.medium: Thermals | {HEAD} size=13 sfsize=13")
        o.append(f"CPU {mm['cpu_c']:.0f} °C  ·  GPU {mm['gpu_c']:.0f} °C  ·  battery {b['cell_c']:.1f} °C | {MONO} {BODY}")
        if mm["fans"]:
            fans = "   ".join(f"{r:.0f} / {mx:.0f}" for r, mx in mm["fans"])
            o.append(f"Fans  {fans} rpm | {MONO} {DIM} sfimage=fanblades")
        lim = thermal_limit()
        if lim < 100:
            o.append(f"Thermal throttling  ·  scheduler limit {lim}% | {MONO} color=#{RED}")
        o.append(area_chart([r[6] for r in hist], 260, 24, ORANGE))
        o.append(f"CPU temperature, last hour · {stats(6, hist)} °C | size=10 {DIM}")
        o.append("---")

    o.append(f":cpu: Top processes by CPU | {HEAD} size=13 sfsize=13 {ACTIVITY_MONITOR}")
    for cpu, name in procs:
        o.append(f"{cpu:5.0f}%  {name[:34]} | {MONO} {DIM} trim=false {ACTIVITY_MONITOR}")
    o.append(f"Open Activity Monitor | {DIM} {ACTIVITY_MONITOR}")
    o.append("---")
    o.append(f"PowerBar itself: {cost:.0f} ms CPU per {SAMPLE_S} s sample  ·  {cost / (SAMPLE_S * 10):.2f}% of one core | size=10 {DIM}")
    return "\n".join(o)

# --- Main loop ----------------------------------------------------------------

def main():
    mm_src = Macmon()
    signal.signal(signal.SIGTERM, lambda *_: (mm_src.stop(), sys.exit(0)))
    hist = load_history()
    def cpu_used():                   # this process plus reaped children (ioreg, ps, pmset)
        t = os.times()
        return t.user + t.system + t.children_user + t.children_system
    cpu_before = cpu_used()
    while True:
        mm = mm_src.read()           # blocks ~SAMPLE_S while macmon samples
        b = battery()
        procs = top_processes()
        hist.append([time.time(), mm["sys_w"] if mm else 0, mm["cpu_w"] if mm else 0,
                     mm["gpu_w"] if mm else 0, b["pct"], b["watts"], mm["cpu_c"] if mm else 0])
        hist = hist[-KEEP:]
        save_history(hist)
        now = cpu_used()
        cost, cpu_before = (now - cpu_before) * 1000, now
        sys.stdout.write(render(mm, b, hist, procs, cost) + "\n~~~\n")
        sys.stdout.flush()
        if not mm:
            time.sleep(SAMPLE_S)

if __name__ == "__main__":
    main()
