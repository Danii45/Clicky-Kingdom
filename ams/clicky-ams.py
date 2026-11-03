#!/usr/bin/python3
"""Clicky AMS: shows what filament is loaded in a Bambu Lab printer's AMS, over your home Wi-Fi.

It only reads. It talks to the printer directly on your network (port 8883, the printer's own
MQTT server) using the access code from the printer's screen. Nothing goes through the internet.

  clicky-ams            opens the window
  clicky-ams --text     prints the slots in the terminal (uses the saved settings)
  clicky-ams --find     looks for printers on your network
"""
import json, os, socket, ssl, sys, threading, time

CONFIG = os.path.expanduser("~/.config/clicky-ams.json")

# The website's colours (Clicky Kingdom FILAMENT list). Each AMS spool is matched to the closest one.
SITE_COLOURS = [
    ("Red", "#d8343a"), ("Orange", "#f07a22"), ("Yellow", "#f5cf2a"), ("Light green", "#8fd96a"),
    ("Green", "#00ae42"), ("Sky blue", "#4aa3f0"), ("Cyan", "#0086d6"), ("Purple", "#5a2fb8"),
    ("Magenta", "#d1338f"), ("Hot pink", "#f5547c"), ("Brown", "#7a4a2c"), ("Gray", "#8e9089"),
    ("Black", "#1e1d24"), ("Matte black", "#2a2a2e"), ("White", "#ecebf3"), ("Matte white", "#e2e1e5"),
]
LOW_PERCENT = 15


# ---------- settings ----------
def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(CONFIG, 0o600)   # the access code is in here, so only you can read it


# ---------- finding the printer ----------
def find_printers(seconds=8):
    """Bambu printers announce themselves on the network every few seconds (SSDP on UDP 1990/2021)."""
    found = {}
    socks = []
    for port in (2021, 1990):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", port))
            try:
                mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0")
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass
            s.settimeout(0.5)
            socks.append(s)
        except OSError:
            pass
    search = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nMAN: \"ssdp:discover\"\r\n"
              "MX: 3\r\nST: urn:bambulab-com:device:3dprinter:1\r\n\r\n").encode()
    for s in socks:
        for port in (1990, 2021):
            try:
                s.sendto(search, ("239.255.255.250", port))
            except OSError:
                pass
    end = time.time() + seconds
    while time.time() < end and socks:
        for s in socks:
            try:
                data, addr = s.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            info = parse_ssdp(data.decode("utf-8", "replace"), addr[0])
            if info:
                found[info["serial"]] = info
    for s in socks:
        s.close()
    return list(found.values())


def parse_ssdp(text, sender_ip):
    if "bambulab" not in text.lower():
        return None
    h = {}
    for line in text.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            h[k.strip().lower()] = v.strip()
    serial = h.get("usn", "").replace("uuid:", "").split("::")[0]
    if not serial:
        return None
    models = {"C11": "P1P", "C12": "P1S", "3DPrinter-X1": "X1", "3DPrinter-X1-Carbon": "X1 Carbon",
              "N1": "A1 mini", "N2S": "A1", "C13": "X1E", "O1D": "H2D"}
    model = h.get("devmodel.bambu.com", "")
    return {"ip": h.get("location", sender_ip) or sender_ip, "serial": serial,
            "name": h.get("devname.bambu.com", "") or "Bambu printer", "model": models.get(model, model)}


# ---------- reading the AMS ----------
def read_printer(ip, serial, code, timeout=15):
    """Connects to the printer's MQTT server, asks for its full status once, and returns the report."""
    import paho.mqtt.client as mqtt
    got, err = {}, {}
    done = threading.Event()
    report_topic, request_topic = f"device/{serial}/report", f"device/{serial}/request"

    def on_connect(c, u, flags, rc, *rest):
        rc = getattr(rc, "value", rc)
        if rc != 0:
            err["msg"] = ("The printer said no. Check the access code (it changes if you turn LAN mode off and on)."
                          if rc in (4, 5, 134, 135) else f"The printer refused the connection (code {rc}).")
            done.set()
            return
        c.subscribe(report_topic)
        c.publish(request_topic, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

    def on_message(c, u, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", "replace"))
        except Exception:
            return
        p = data.get("print") or {}
        if "ams" in p or "vt_tray" in p:
            got.update(p)
            done.set()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"clicky-ams-{os.getpid()}")
    except AttributeError:   # paho-mqtt 1.x (Ubuntu 24.04)
        client = mqtt.Client(client_id=f"clicky-ams-{os.getpid()}")
    client.username_pw_set("bblp", code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # the printer uses its own self-signed certificate
    client.tls_set_context(ctx)
    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(ip, 8883, keepalive=30)
    except Exception as e:
        raise RuntimeError(f"Can't reach the printer at {ip}. Is it on, and on the same Wi-Fi as this computer? ({e})")
    client.loop_start()
    try:
        if not done.wait(timeout):
            raise RuntimeError("The printer didn't answer. Check the serial number, and that the printer isn't asleep.")
        if "msg" in err:
            raise RuntimeError(err["msg"])
        return got
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass


def hex_rgb(h):
    h = h.lstrip("#")[:6]
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else (128, 128, 128)


def site_match(hexcol, matte):
    r, g, b = hex_rgb(hexcol)
    best = min(SITE_COLOURS, key=lambda c: sum((x - y) ** 2 for x, y in zip((r, g, b), hex_rgb(c[1]))))[0]
    if matte and best in ("Black", "White"):
        best = "Matte " + best.lower()
    if not matte and best.startswith("Matte "):
        best = best[6:].capitalize()
    return best


def slots_from(report):
    """Turns the printer's report into a simple list of slots."""
    ams = report.get("ams") or {}
    now = str(ams.get("tray_now", ""))
    out = []
    for unit in ams.get("ams") or []:
        u = int(unit.get("id", 0))
        for t in unit.get("tray") or []:
            i = int(t.get("id", 0))
            out.append(slot(t, f"AMS {u + 1} · slot {i + 1}" if len(ams.get("ams")) > 1 else f"Slot {i + 1}",
                            now == str(u * 4 + i)))
    vt = report.get("vt_tray")
    if vt and vt.get("tray_type"):
        out.append(slot(vt, "External spool", now == "254"))
    return out


def slot(t, where, active):
    kind = t.get("tray_type") or ""
    if not kind:
        return {"where": where, "empty": True, "active": active}
    colour = "#" + (t.get("tray_color") or "808080FF")[:6]
    sub = t.get("tray_sub_brands") or ""
    try:
        remain = int(t.get("remain", -1))
    except (TypeError, ValueError):
        remain = -1
    matte = "matte" in sub.lower()
    return {"where": where, "empty": False, "active": active, "type": kind, "name": sub or kind,
            "colour": colour, "remain": remain, "low": 0 <= remain <= LOW_PERCENT,
            "site": site_match(colour, matte)}


def describe(s):
    if s["empty"]:
        return f"{s['where']}: empty"
    left = f"{s['remain']}% left" if s["remain"] >= 0 else "amount unknown (no RFID tag)"
    flags = (" · LOW" if s["low"] else "") + (" · printing with this now" if s["active"] else "")
    return f"{s['where']}: {s['name']} {s['colour']} · {left} · website colour: {s['site']}{flags}"


# ---------- terminal mode ----------
def main_text():
    cfg = load_config()
    if "--find" in sys.argv:
        print("Looking for printers on your Wi-Fi (about 8 seconds)…")
        ps = find_printers()
        for p in ps:
            print(f"  {p['name']} ({p['model']}) at {p['ip']}, serial {p['serial']}")
        if not ps:
            print("  None found. Type the printer's IP address in the app instead.")
        return
    if not all(cfg.get(k) for k in ("ip", "serial", "code")):
        print("Open Clicky AMS once and fill in the printer settings first.")
        sys.exit(1)
    for s in slots_from(read_printer(cfg["ip"], cfg["serial"], cfg["code"])):
        print(describe(s))


# ---------- the window ----------
def main_window():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib, Gdk

    cfg = load_config()
    GLib.set_prgname("clicky-ams")
    GLib.set_application_name("Clicky AMS")

    css = Gtk.CssProvider()
    css.load_from_data(b"""
      .title { font-size: 22px; font-weight: 800; }
      .muted { opacity: .7; }
      .bad { color: #d8343a; }
      .slot { padding: 10px 6px; }
      .slotname { font-weight: 700; font-size: 15px; }
      .low { background: #e0531b; color: white; border-radius: 6px; padding: 1px 6px; font-weight: 700; font-size: 11px; }
      .now { background: #5a2fb8; color: white; border-radius: 6px; padding: 1px 6px; font-weight: 700; font-size: 11px; }
    """)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    win = Gtk.Window(title="Clicky AMS")
    win.set_default_size(560, 640)
    icon = os.path.expanduser("~/.local/share/clicky-ams/icon.png")
    if os.path.exists(icon):
        try:
            win.set_icon_from_file(icon)
        except Exception:
            pass
    win.connect("destroy", Gtk.main_quit)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=18)
    win.add(outer)
    title = Gtk.Label(label="What's in the AMS", xalign=0)
    title.get_style_context().add_class("title")
    outer.pack_start(title, False, False, 0)
    status = Gtk.Label(xalign=0, wrap=True)
    status.get_style_context().add_class("muted")
    outer.pack_start(status, False, False, 0)

    # settings
    exp = Gtk.Expander(label="Printer settings")
    grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin_top=8)
    exp.add(grid)
    entries = {}
    rows = [("ip", "Printer IP address", "e.g. 192.168.1.50"),
            ("serial", "Serial number", "Printer screen: Settings → Device"),
            ("code", "Access code", "Printer screen: Settings → Network")]
    for r, (key, label, hint) in enumerate(rows):
        grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
        e = Gtk.Entry(text=cfg.get(key, ""), placeholder_text=hint, hexpand=True)
        if key == "code":
            e.set_visibility(False)
        entries[key] = e
        grid.attach(e, 1, r, 1, 1)
    btns = Gtk.Box(spacing=8)
    find_btn = Gtk.Button(label="Find my printer")
    save_btn = Gtk.Button(label="Save")
    btns.pack_start(find_btn, False, False, 0)
    btns.pack_start(save_btn, False, False, 0)
    grid.attach(btns, 1, len(rows), 1, 1)
    outer.pack_start(exp, False, False, 0)

    check_btn = Gtk.Button(label="Check the AMS")
    outer.pack_start(check_btn, False, False, 0)

    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    scroll = Gtk.ScrolledWindow(vexpand=True)
    scroll.add(listbox)
    outer.pack_start(scroll, True, True, 0)

    def set_status(text, bad=False):
        status.set_text(text)
        ctx = status.get_style_context()
        (ctx.add_class if bad else ctx.remove_class)("bad")

    def swatch(hexcol, empty):
        area = Gtk.DrawingArea()
        area.set_size_request(40, 40)
        r, g, b = (x / 255 for x in hex_rgb(hexcol or "#888888"))

        def draw(w, cr):
            cr.arc(20, 20, 17, 0, 6.2832)
            if empty:
                cr.set_source_rgba(.5, .5, .5, .6)
                cr.set_dash([3, 3])
                cr.set_line_width(2)
                cr.stroke()
            else:
                cr.set_source_rgb(r, g, b)
                cr.fill_preserve()
                cr.set_source_rgba(.5, .5, .5, .5)
                cr.set_line_width(1)
                cr.stroke()
        area.connect("draw", draw)
        return area

    def show_slots(slots):
        for child in listbox.get_children():
            listbox.remove(child)
        for s in slots:
            row = Gtk.Box(spacing=12)
            row.get_style_context().add_class("slot")
            row.pack_start(swatch(s.get("colour"), s["empty"]), False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            top = Gtk.Box(spacing=6)
            name = Gtk.Label(label=s["where"] + ("  ·  empty" if s["empty"] else f"  ·  {s['name']}"), xalign=0)
            name.get_style_context().add_class("slotname")
            top.pack_start(name, False, False, 0)
            for flag, cls in ((s.get("low"), "low"), (s.get("active"), "now")):
                if flag:
                    tag = Gtk.Label(label="LOW" if cls == "low" else "PRINTING")
                    tag.get_style_context().add_class(cls)
                    top.pack_start(tag, False, False, 0)
            text.pack_start(top, False, False, 0)
            if not s["empty"]:
                left = f"{s['remain']}% left" if s["remain"] >= 0 else "Amount unknown (no RFID tag)"
                sub = Gtk.Label(label=f"{left}  ·  website colour: {s['site']}  ·  {s['colour']}", xalign=0)
                sub.get_style_context().add_class("muted")
                text.pack_start(sub, False, False, 0)
            row.pack_start(text, True, True, 0)
            listbox.add(row)
        listbox.show_all()

    def current():
        return {k: e.get_text().strip() for k, e in entries.items()}

    def do_save(*_):
        cfg.update(current())
        save_config(cfg)
        set_status("Saved.")

    busy = {"on": False}

    def do_check(*_):
        c = current()
        if not all(c.values()):
            exp.set_expanded(True)
            set_status("Fill in the printer settings first. “Find my printer” fills in the IP and serial.", True)
            return
        if busy["on"]:
            return
        busy["on"] = True
        check_btn.set_sensitive(False)
        set_status("Asking the printer…")

        def work():
            try:
                slots = slots_from(read_printer(c["ip"], c["serial"], c["code"]))
                def ok():
                    show_slots(slots)
                    low = [s for s in slots if s.get("low")]
                    set_status(f"Updated {time.strftime('%-I:%M %p')}. " +
                               (f"Running low: {', '.join(s['name'] + ' (' + s['site'] + ')' for s in low)}." if low else "Nothing is running low.") +
                               " Checks again every minute.")
                GLib.idle_add(ok)
                cfg.update(c)
                save_config(cfg)
            except Exception as e:
                msg = str(e) if isinstance(e, RuntimeError) else f"Something went wrong: {e}"
                GLib.idle_add(lambda: set_status(msg, True))
            finally:
                def done():
                    busy["on"] = False
                    check_btn.set_sensitive(True)
                GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def do_find(*_):
        find_btn.set_sensitive(False)
        set_status("Looking for your printer on the Wi-Fi (about 8 seconds)…")

        def work():
            ps = find_printers()
            def show():
                find_btn.set_sensitive(True)
                if not ps:
                    set_status("Couldn't find it automatically. Type the IP address from the printer's screen "
                               "(Settings → Network) and the serial number (Settings → Device).", True)
                    return
                p = ps[0]
                entries["ip"].set_text(p["ip"])
                entries["serial"].set_text(p["serial"])
                set_status(f"Found {p['name']} ({p['model']}) at {p['ip']}. Now type the access code and press “Check the AMS”.")
                entries["code"].grab_focus()
            GLib.idle_add(show)
        threading.Thread(target=work, daemon=True).start()

    find_btn.connect("clicked", do_find)
    save_btn.connect("clicked", do_save)
    check_btn.connect("clicked", do_check)

    win.show_all()
    if all(cfg.get(k) for k in ("ip", "serial", "code")):
        do_check()
    else:
        exp.set_expanded(True)
        set_status("First time: press “Find my printer”, then type the access code from the printer's screen.")

    def tick():
        if all(current().values()):
            do_check()
        return True
    GLib.timeout_add_seconds(60, tick)
    Gtk.main()


if __name__ == "__main__":
    if "--text" in sys.argv or "--find" in sys.argv:
        try:
            main_text()
        except RuntimeError as e:
            print(e)
            sys.exit(1)
    else:
        main_window()
