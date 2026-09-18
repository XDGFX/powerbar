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

import base64, json, math, os, re, shutil, signal, struct, subprocess, sys, time, zlib

SAMPLE_S = 10                       # seconds between samples
KEEP = 360                          # samples kept: 1 h at 10 s
STATE = os.path.expanduser("~/.local/state/powerbar")
HISTORY = os.path.join(STATE, "history.tsv")
os.makedirs(STATE, exist_ok=True)

# Colours: "light,dark". An explicit colour also keeps SwiftBar from rendering
# a row in the disabled grey — but it costs the row its inertness. SwiftBar
# attaches an action to anything carrying color= so macOS draws it enabled, so
# every coloured row highlights under the pointer whether or not it opens
# anything. Dropping the colour to suppress that fades the text far too much to
# be worth it, so the highlight is simply lived with.
HEAD = "color=#1d1d1f,#f5f5f7"
BODY = "color=#3a3a3c,#e5e5ea"
DIM = "color=#8e8e93,#8e8e93"
# Data rows are set on a character grid so a value never moves when it changes
# width — the whole point of a monitor you read at a glance. That needs a fixed
# pitch font, because the system font's digits are not tabular (0 is 8.1 pt
# wide, 1 is 6.0) and SwiftBar's only other column tool is a tab stop hardcoded
# every 100 pt, which is far too coarse.
#
# Monaco at 12 pt is the narrowest fixed-pitch face that also sits right: a
# row's text area is 16 pt, the line height of the 13 pt system font AppKit
# sizes the menu from, and a shorter line is set at the top of that area rather
# than the middle, so anything shorter rides high. Monaco 12 sets exactly 16 pt
# and lands centred with no correction at all. (JetBrainsMono-Regular 12 has
# identical metrics if it is installed and you prefer the shapes — swapping the
# name here changes nothing else.) Captions are small enough to need the nudge:
# valign is added straight to .baselineOffset, negative is down, so half the
# 3 pt slack brings them back.
MONO = "font=Monaco size=12"                 # Monaco 12 sets a 16 pt line
PITCH = 7.2                                  # ... at 7.20 pt per character
LBL = 10                                     # label column, in grid characters
CAPTION = "size=11 valign=-1.5"              # the system font at 11 sets 13 pt
BLUE, GREEN, AMBER, RED = "0a84ff", "30d158", "ffd60a", "ff453a"
ORANGE, PURPLE, TEAL, GREY = "ff9f0a", "bf5af2", "64d2ff", "636366"

# Every row that reports a measurement opens the app that owns it; the labels
# and the charts do not. The settings URL takes the pane's bundle identifier,
# which for Battery is com.apple.Battery-Settings.extension — with a dot before
# "extension", not the hyphen that lands you on General instead.
BATTERY_SETTINGS = "bash=/usr/bin/open param1=x-apple.systempreferences:com.apple.Battery-Settings.extension terminal=false"
ACTIVITY_MONITOR = "bash=/usr/bin/open param1=-a param2='Activity Monitor' terminal=false"

# Symbols are carried inline (:name:) rather than as sfimage=: an sfimage is set
# on the item separately from its title, so SwiftBar tints it with the row's
# light-mode colour — which vanishes on a dark menu — and never re-tints it when
# the row highlights, so it disappears under the cursor. An inline symbol is
# part of the attributed title and follows the text through both.

# --- PNG drawing (pure stdlib) ----------------------------------------------
# Everything is drawn at 2x and told to SwiftBar at 1x (width=/height=) so it is
# crisp on Retina. Edges are antialiased analytically — each pixel's coverage is
# derived from where the trace falls inside it, rather than by supersampling —
# and the pixels themselves come out of small lookup tables, so the whole set of
# charts costs about a millisecond.
#
# The look follows Battery Settings: a translucent area under a bright stroke,
# dotted rules quartering the plot, a solid axis along the bottom, and (on the
# charge chart) the periods spent on mains tinted behind the trace.

S = 2                               # device pixels per point
LINE_PX = 2.0                       # trace stroke width, device pixels
LINE_A = 0.95                       # trace opacity
FILL_TOP, FILL_BOT = 0.36, 0.05     # area gradient at the top / bottom of the plot
FILL_STEPS = 8                      # banded, so one table serves several scanlines
Q = 8                               # sub-pixel steps used for antialiasing
DIVS = 4                            # grid divisions on both axes
GRID = "8e8e93"                     # a grey that reads on either appearance
GRID_A, AXIS_A, BAND_A, TRACK_A = 0.20, 0.32, 0.12, 0.28
CLEAR = b"\x00\x00\x00\x00"


def png(w, h, rows):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    ).decode()


def rgb(hexc):
    return bytes((int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)))


def over(fg, fa, bg, ba):
    """Composite straight-alpha fg over straight-alpha bg into an RGBA pixel."""
    a = fa + ba * (1.0 - fa)
    if a <= 0.002:
        return CLEAR
    j, k = fa / a, ba * (1.0 - fa) / a
    return bytes((int(fg[0] * j + bg[0] * k + 0.5), int(fg[1] * j + bg[1] * k + 0.5),
                  int(fg[2] * j + bg[2] * k + 0.5), int(a * 255.0 + 0.5)))


def fill_table(c, fill_a, back, back_a, below, above):
    """Area pixels indexed by a scanline's offset from the trace, in 1/Q of a px.

    Only the offsets the trace's own top edge straddles are computed; anything
    further away is one of two constants, so the table is mostly padding and
    costs nothing to widen to the full height of the plot.
    """
    edge = [over(c, fill_a * min(1.0, -k / Q), back, back_a) for k in range(-Q, 1)]
    return [over(c, fill_a, back, back_a)] * (below - Q) + edge \
        + [over(c, 0.0, back, back_a)] * above


def grid(rows, w, h):
    """Dotted rules behind the trace, and a solid axis along the bottom."""
    g = rgb(GRID)
    dots = [i for i in range(max(w, h)) if i % 6 < 2]      # 2 on, 4 off, a 1 pt rule
    xdots = [x for x in dots if x < w]
    def under(y, xs, ga):
        row = rows[y]
        for x in xs:
            i = x * 4
            fa = row[i + 3] / 255.0
            if fa > 0.985:
                continue
            row[i:i + 4] = over(bytes(row[i:i + 3]), fa, g, ga)
    for i in range(1, DIVS):
        y = h - 1 - int(h * i / DIVS)
        for yy in (y, y + 1):
            if 0 <= yy < h:
                under(yy, xdots, GRID_A)
        x0 = int(w * i / DIVS)
        xs = [x for x in (x0, x0 + 1) if 0 <= x < w]
        for yy in dots:
            if yy < h:
                under(yy, xs, GRID_A)
    for yy in (h - 1, h - 2):
        under(yy, range(w), AXIS_A)


def stroke(rows, ys, w, h, c):
    """Draw the trace as a connected polyline over the area.

    Each column paints the slice of the line between the midpoints it shares
    with its neighbours, so a steep step is a continuous vertical run rather
    than two detached dabs — which is what made spiky data look like speckle.
    """
    half = LINE_PX / 2.0
    cache = {}
    for x in range(w):
        y = ys[x]
        a = (ys[x - 1] + y) / 2.0 if x else y
        b = (y + ys[x + 1]) / 2.0 if x + 1 < w else y
        if a > b:
            a, b = b, a
        a, b = min(a, y) - half, max(b, y) + half
        i = x * 4
        for d in range(max(0, int(a)), min(h - 1, int(b)) + 1):
            cov = min(d + 1.0, b) - max(float(d), a)
            if cov <= 0.0:
                continue
            row = rows[h - 1 - d]
            key = (bytes(row[i:i + 4]), int(cov * 24.0))
            px = cache.get(key)
            if px is None:
                under = key[0]
                px = cache[key] = over(c, LINE_A * min(cov, 1.0), under[:3], under[3] / 255.0)
            row[i:i + 4] = px


def area_chart(vals, W, H, hexc, lo=None, hi=None, bands=None, band_hex=None):
    """Filled area chart of the whole history, latest sample at the right edge.

    `bands` marks samples to tint behind the trace (mains, on the charge chart).
    """
    w, h = W * S, H * S
    c, bc = rgb(hexc), rgb(band_hex or hexc)
    if not vals:
        vals = [0.0]
    vmin, vmax = min(vals), max(vals)
    if lo is None:
        lo = max(0.0, vmin - 0.25 * (vmax - vmin or 1))
    if hi is None:
        hi = vmax + 0.1 * (vmax - vmin or 1)
    span = max(hi - lo, 1e-6)
    # whatever history exists is stretched across the full width, latest at the
    # right edge; the caption says how long the window is. Samples are read off
    # with linear interpolation rather than nearest-neighbour, so a sample that
    # spans several columns slopes between its neighbours instead of stepping.
    n, top = len(vals), h - LINE_PX / 2.0 - 1.0     # keep the stroke in the canvas
    pitch = (n - 1) / (w - 1) if w > 1 and n > 1 else 0.0
    ys = []
    for x in range(w):
        p = x * pitch
        i = int(p)
        v = vals[i] if i + 1 >= n else vals[i] + (vals[i + 1] - vals[i]) * (p - i)
        ys.append((min(max(v, lo), hi) - lo) / span * top)
    qy = [int(y * Q + 0.5) for y in ys]
    cols = None
    if bands:
        cols = list(zip([1 if bands[min(int(x * pitch), n - 1)] else 0 for x in range(w)], qy))

    # The area first: every scanline's pixel is a table lookup on how far it
    # sits under the trace, so a whole row is one join with no arithmetic.
    below, above = max(max(qy), Q) + Q, h * Q
    tables, rows = {}, []
    for y in range(h):
        d = h - 1 - y
        step = d * FILL_STEPS // h
        t = tables.get(step)
        if t is None:
            f = FILL_BOT + (FILL_TOP - FILL_BOT) * (step + 0.5) / FILL_STEPS
            t = tables[step] = (fill_table(c, f, bc, 0.0, below, above),
                                fill_table(c, f, bc, BAND_A, below, above) if cols else None)
        base = d * Q + below
        if cols is None:
            plain = t[0]
            rows.append(bytearray(b"".join([plain[base - q] for q in qy])))
        else:
            rows.append(bytearray(b"".join([t[b][base - q] for b, q in cols])))
    grid(rows, w, h)
    stroke(rows, ys, w, h, c)
    return f"| image={png(w, h, rows)} width={W} height={H}"


def stacked_bar(parts, W, H):
    """Horizontal stacked bar: a capsule track, hairline gaps between segments."""
    w, h = W * S, H * S
    r = h / 2.0
    track = rgb(GREY)
    seg, x = [], 0.0
    for frac, hexc in parts:
        x1 = x + max(0.0, frac) * w
        x0i, x1i = max(0, int(x + 0.5)), min(w, int(x1 + 0.5))
        if x1i > x0i:
            seg.append([x0i, x1i, rgb(hexc)])
        x = x1
    for i in range(len(seg) - 1):        # a gap between neighbours, not after the last
        seg[i][1] = max(seg[i][0] + 1, seg[i][1] - S)
    cols = [None] * w
    for x0i, x1i, c in seg:
        for xx in range(x0i, x1i):
            cols[xx] = c
    solid = {c: over(c, 1.0, track, 0.0) for c in cols if c is not None}
    solid[None] = over(track, TRACK_A, track, 0.0)
    rows = []
    for y in range(h):
        dy = abs(y + 0.5 - r)
        ins = r - math.sqrt(max(r * r - dy * dy, 0.0))    # capsule inset at this scanline
        end = int(ins) + 1                                # only the ends need coverage
        row = [solid[c] for c in cols]
        for xx in list(range(min(end, w))) + list(range(max(0, w - end), w)):
            e = min(xx + 0.5 - ins, (w - ins) - xx - 0.5)
            cov = 0.0 if e <= -0.5 else (1.0 if e >= 0.5 else e + 0.5)
            c = cols[xx]
            row[xx] = over(track, TRACK_A * cov, track, 0.0) if c is None else over(c, cov, track, 0.0)
        rows.append(bytearray(b"".join(row)))
    return f"| image={png(w, h, rows)} width={W} height={H}"

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
    o, art = [], []
    on_ac = b["external"]

    # Charts are held back until every row is known, then drawn exactly as wide
    # as the widest of them, so they span the menu instead of stopping short.
    def chart(vals, H, hexc, **kw):
        art.append((len(o), area_chart, vals, H, (hexc,), kw))
        o.append("")

    def bar(parts, H):
        art.append((len(o), stacked_bar, parts, H, (), {}))
        o.append("")

    # menu bar
    if mm:
        o.append(f":bolt.fill: {mm['sys_w']:.0f} W | sfsize=12" if on_ac
                 else f":battery.50percent: {b['pct']}% | sfsize=12")
    else:
        o.append(f":battery.50percent: {b['pct']}% | sfsize=12")
    o.append("---")

    if mm:
        o.append(f":bolt.circle.fill: Power | {HEAD} size=13 sfsize=13")
        o.append(f"{'System':<{LBL}}{mm['sys_w']:>5.1f} W | {MONO} {BODY}")
        o.append(f"{'Package':<{LBL}}{mm['pkg_w']:>5.1f} W | {MONO} {BODY}")
        pk = max(mm["pkg_w"], 1e-6)
        o.append(f"{'CPU':<{LBL}}{mm['cpu_w']:>5.1f} W · {mm['cpu_pct']:>3.0f}% · E {mm['e_pct']:>3.0f}% {mm['e_mhz']:>4.0f} · P {mm['p_pct']:>3.0f}% {mm['p_mhz']:>4.0f} MHz | {MONO} {DIM}")
        o.append(f"{'GPU':<{LBL}}{mm['gpu_w']:>5.1f} W · {mm['gpu_pct']:>3.0f}% @ {mm['gpu_mhz']:>4.0f} MHz | {MONO} {DIM}")
        o.append(f"{'ANE':<{LBL}}{mm['ane_w']:>5.1f} W | {MONO} {DIM}")
        o.append(f"{'RAM':<{LBL}}{mm['ram_w']:>5.1f} W · {mm['ram_pct']:>3.0f}% used · swap {mm['swap_gb']:>4.1f} GB | {MONO} {DIM}")
        bar([(mm["cpu_w"] / pk, BLUE), (mm["gpu_w"] / pk, PURPLE),
             (mm["ane_w"] / pk, TEAL), (mm["ram_w"] / pk, ORANGE)], 6)
        o.append(f"CPU · GPU · ANE · RAM share of package | {CAPTION} {DIM}")
        chart([r[1] for r in hist], 40, BLUE)
        o.append(f"System power, last hour · {stats(1, hist)} W | {CAPTION} {DIM}")
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
        o.append(f"{'From pack':<{LBL}}{-b['watts']:>5.1f} W · {eta(b['mins']):>7} remaining | {MONO} color=#{col} {BATTERY_SETTINGS}")
    elif b["charging"]:
        o.append(f"{'Into pack':<{LBL}}{b['watts']:>5.1f} W · full in {eta(b['mins']):>7} | {MONO} color=#{col} {BATTERY_SETTINGS}")
    o.append(f"{'Health':<{LBL}}{b['health']:>4}% · {b['raw_max']:>5} / {b['design']} mAh · {b['cycles']:>4} cycles | {MONO} {DIM} {BATTERY_SETTINGS}")
    o.append(f"{'Cell':<{LBL}}{b['cell_c']:>5.1f} °C · {b['mv']/1000:>5.2f} V · {b['ma']:>6} mA | {MONO} {DIM} {BATTERY_SETTINGS}")
    if b["adapter"]:
        o.append(f"{'Adapter':<{LBL}}{b['adapter_w']:>4} W · {b['adapter']} | {MONO} {DIM} {BATTERY_SETTINGS}")
    chart([r[4] for r in hist], 24, col, lo=0, hi=100,
          bands=[r[5] > 0 for r in hist], band_hex=GREEN)
    o.append(f"Charge, last hour · shaded while charging | {CAPTION} {DIM}")
    o.append("---")

    if mm:
        o.append(f":thermometer.medium: Thermals | {HEAD} size=13 sfsize=13")
        o.append(f"{'CPU':<{LBL}}{mm['cpu_c']:>3.0f} °C · GPU {mm['gpu_c']:>3.0f} °C · pack {b['cell_c']:>5.1f} °C | {MONO} {BODY}")
        if mm["fans"]:
            fans = " · ".join(f"{r:>4.0f} / {mx:>4.0f}" for r, mx in mm["fans"])
            o.append(f"{'Fans':<{LBL}}{fans} rpm | {MONO} {DIM}")
        lim = thermal_limit()
        if lim < 100:
            o.append(f":exclamationmark.triangle.fill: Thermal throttling · scheduler limit {lim}% | {MONO} color=#{RED} sfsize=12")
        chart([r[6] for r in hist], 24, ORANGE)
        o.append(f"CPU temperature, last hour · {stats(6, hist)} °C | {CAPTION} {DIM}")
        o.append("---")

    o.append(f":cpu: Top processes by CPU | {HEAD} size=13 sfsize=13 {ACTIVITY_MONITOR}")
    for cpu, name in procs:
        o.append(f"{cpu:>6.0f}%   {name[:32]} | {MONO} {DIM} trim=false {ACTIVITY_MONITOR}")
    o.append(f"Open Activity Monitor | {DIM} {ACTIVITY_MONITOR}")
    o.append("---")
    o.append(f"PowerBar itself: {cost:.0f} ms CPU per {SAMPLE_S} s sample  ·  {cost / (SAMPLE_S * 10):.2f}% of one core | {CAPTION} {DIM}")

    cells = max((len(r.split(" | ")[0]) for r in o if MONO in r), default=52)
    W = int(cells * PITCH)
    for i, draw, data, H, rest, kw in art:
        o[i] = draw(data, W, H, *rest, **kw)
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
