#!/usr/bin/python3
"""Clicky AMS: a filament dashboard for a Bambu Lab printer, over your home Wi-Fi.

It remembers your printer, stays connected while open, and shows the printer's status, what's in the
AMS, and all your spools (including ones on the shelf). It only reads from the printer, using the
access code from the printer's screen, and nothing goes through the internet.

  clicky-ams            opens the dashboard
  clicky-ams --text     prints the AMS slots in the terminal
  clicky-ams --find     looks for printers on your network
"""
import json, os, socket, ssl, sys, threading, time

HERE = os.path.dirname(os.path.realpath(__file__))
CONFIG = os.path.expanduser("~/.config/clicky-ams.json")

# Starting list of spools (from Daniel's Bambu filament library, Nov 2026). Edited in the app after that.
STARTER_SPOOLS = [
    {"name": "Charcoal", "type": "PLA Matte", "brand": "Bambu Lab", "hex": "#000000", "grams": 650},
    {"name": "Gray", "type": "PLA Basic", "brand": "Bambu Lab", "hex": "#8e9089", "grams": 1000},
    {"name": "Bambu Green", "type": "PLA Basic", "brand": "Bambu Lab", "hex": "#00ae42", "grams": 43},
    {"name": "Orange", "type": "PLA Basic", "brand": "Bambu Lab", "hex": "#ff6a13", "grams": 40},
    {"name": "Orange", "type": "PLA Basic", "brand": "Bambu Lab", "hex": "#f98c36", "grams": 1000},
    {"name": "Grey (came with printer)", "type": "PLA Basic", "brand": "Bambu Lab", "hex": "#c8c8c8", "grams": 1000, "noSite": True},
    {"name": "Dark Brown", "type": "PLA", "brand": "Generic", "hex": "#7d4e00", "grams": 1000},
    {"name": "Red", "type": "PLA", "brand": "Generic", "hex": "#f72323", "grams": 1000},
    {"name": "Cyan", "type": "PLA Basic", "brand": "Bambu Lab", "hex": "#0086d6", "grams": 1000},
    {"name": "Ivory White", "type": "PLA Matte", "brand": "Bambu Lab", "hex": "#fffff0", "grams": 1000},
    {"name": "White", "type": "Support for PLA", "brand": "Bambu Lab", "hex": "#ffffff", "grams": 35, "support": True},
]


# ---------- settings ----------
def load_config():
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if "spools" not in cfg:
        cfg["spools"] = [dict(s, id=f"s{i + 1}") for i, s in enumerate(STARTER_SPOOLS)]
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, 0o600)   # the access code is in here, so only you can read it
    os.replace(tmp, CONFIG)


# ---------- finding the printer ----------
def find_printers(seconds=8):
    """Bambu printers announce themselves on the network every few seconds (SSDP on UDP 1990/2021)."""
    found, socks = {}, []
    for port in (2021, 1990):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", port))
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0"))
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


# ---------- talking to the printer ----------
def make_client(code):
    import paho.mqtt.client as mqtt
    cid = f"clicky-ams-{os.getpid()}-{int(time.time()) % 10000}"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    except AttributeError:   # paho-mqtt 1.x (Ubuntu 24.04)
        client = mqtt.Client(client_id=cid)
    client.username_pw_set("bblp", code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # the printer uses its own self-signed certificate
    client.tls_set_context(ctx)
    return client


def refused_message(rc):
    rc = getattr(rc, "value", rc)
    if rc in (4, 5, 134, 135):
        return "The printer said no. Check the access code (it changes if you turn LAN mode off and on)."
    return f"The printer refused the connection (code {rc})."


def merge(dst, src):
    """The printer sends small updates; fold them into the full picture."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v


def read_printer(ip, serial, code, timeout=15):
    """One-off read (used by --text)."""
    got, err, done = {}, {}, threading.Event()

    def on_connect(c, u, flags, rc, *rest):
        if getattr(rc, "value", rc) != 0:
            err["msg"] = refused_message(rc)
            done.set()
            return
        c.subscribe(f"device/{serial}/report")
        c.publish(f"device/{serial}/request", json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

    def on_message(c, u, msg):
        try:
            p = json.loads(msg.payload.decode("utf-8", "replace")).get("print") or {}
        except Exception:
            return
        if "ams" in p or "vt_tray" in p:
            got.update(p)
            done.set()

    client = make_client(code)
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


class LiveLink:
    """Stays connected to the printer and calls on_update(state) whenever something changes."""

    def __init__(self, on_update):
        self.on_update = on_update
        self.client = None
        self.report = {}
        self.settings = None
        self.lock = threading.Lock()
        self.last_push = 0
        self.last_sent = 0
        self.pending = False
        self.status = {"connected": False, "error": "", "connecting": False}

    def start(self, ip, serial, code):
        self.stop()
        if not (ip and serial and code):
            return
        self.settings = (ip, serial, code)
        self.report = {}
        self.status = {"connected": False, "error": "", "connecting": True}
        self.emit(force=True)
        threading.Thread(target=self._run, args=(ip, serial, code), daemon=True).start()

    def _run(self, ip, serial, code):
        client = make_client(code)
        self.client = client

        def on_connect(c, u, flags, rc, *rest):
            if getattr(rc, "value", rc) != 0:
                self.status = {"connected": False, "error": refused_message(rc), "connecting": False}
                self.emit(force=True)
                c.disconnect()
                return
            self.status = {"connected": True, "error": "", "connecting": False}
            c.subscribe(f"device/{serial}/report")
            self.pushall()
            self.emit(force=True)

        def on_disconnect(c, u, *rest):
            if self.client is client and self.status.get("connected"):
                self.status = {"connected": False, "error": "Lost the connection to the printer. Trying again…", "connecting": True}
                self.emit(force=True)

        def on_message(c, u, msg):
            try:
                p = json.loads(msg.payload.decode("utf-8", "replace")).get("print")
            except Exception:
                return
            if isinstance(p, dict):
                with self.lock:
                    merge(self.report, p)
                self.emit()

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.reconnect_delay_set(2, 30)
        while self.client is client:
            try:
                client.connect(ip, 8883, keepalive=30)
                client.loop_forever(retry_first_connection=True)
                break
            except Exception as e:
                self.status = {"connected": False, "connecting": False,
                               "error": f"Can't reach the printer at {ip}. Is it on, and on the same Wi-Fi as this computer?"}
                self.emit(force=True)
                for _ in range(30):   # try again in 30 seconds
                    if self.client is not client:
                        return
                    time.sleep(1)

    def pushall(self):
        c, s = self.client, self.settings
        if c and s:
            self.last_push = time.time()
            c.publish(f"device/{s[1]}/request", json.dumps({"pushing": {"sequence_id": str(int(time.time())), "command": "pushall"}}))

    def tick(self):
        """Every so often, ask for the full picture again (not too often: it's heavy for P1 printers)."""
        if self.status.get("connected") and time.time() - self.last_push > 300:
            self.pushall()

    def stop(self):
        c, self.client = self.client, None
        if c:
            try:
                c.disconnect()
            except Exception:
                pass

    def emit(self, force=False):
        now = time.time()
        if not force and now - self.last_sent < 0.8:   # at most about once a second
            if not self.pending:
                self.pending = True
                threading.Timer(1.0, self._flush).start()
            return
        self.last_sent = now
        with self.lock:
            snap = summarize(self.report)
        snap.update(self.status)
        self.on_update(snap)

    def _flush(self):
        self.pending = False
        self.emit(force=True)


def summarize(p):
    """Turns the printer's report into what the dashboard shows."""
    ams = p.get("ams") or {}
    now = str(ams.get("tray_now", ""))
    units = ams.get("ams") or []
    slots = []
    for unit in units:
        u = int(unit.get("id", 0))
        for t in unit.get("tray") or []:
            i = int(t.get("id", 0))
            where = f"AMS {u + 1} · {i + 1}" if len(units) > 1 else f"Slot {i + 1}"
            slots.append(tray_info(t, where, now == str(u * 4 + i), f"{u}-{i}"))
    vt = p.get("vt_tray")
    if isinstance(vt, dict) and vt.get("tray_type"):
        slots.append(tray_info(vt, "External", now == "254", "ext"))

    def num(k):
        try:
            return float(p.get(k))
        except (TypeError, ValueError):
            return None

    return {
        "hasData": bool(p),
        "ams": slots,
        "amsHumidity": (units[0].get("humidity") if units else None),
        "job": {
            "state": p.get("gcode_state") or "",
            "percent": num("mc_percent"),
            "minutesLeft": num("mc_remaining_time"),
            "name": p.get("subtask_name") or "",
            "layer": num("layer_num"), "layers": num("total_layer_num"),
            "nozzle": num("nozzle_temper"), "nozzleTarget": num("nozzle_target_temper"),
            "bed": num("bed_temper"), "bedTarget": num("bed_target_temper"),
            "wifi": p.get("wifi_signal") or "",
        },
    }


def tray_info(t, where, active, key):
    kind = t.get("tray_type") or ""
    if not kind:
        return {"key": key, "where": where, "empty": True, "active": active}
    try:
        remain = int(t.get("remain", -1))
    except (TypeError, ValueError):
        remain = -1
    try:
        weight = int(float(t.get("tray_weight") or 1000))
    except (TypeError, ValueError):
        weight = 1000
    return {"key": key, "where": where, "empty": False, "active": active, "type": kind,
            "name": t.get("tray_sub_brands") or kind, "hex": "#" + (t.get("tray_color") or "808080FF")[:6].lower(),
            "remain": remain, "weight": weight or 1000}


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
    pr = cfg.get("printer") or {}
    if not all(pr.get(k) for k in ("ip", "serial", "code")):
        print("Open Clicky AMS once and connect your printer first.")
        sys.exit(1)
    for s in summarize(read_printer(pr["ip"], pr["serial"], pr["code"]))["ams"]:
        if s["empty"]:
            print(f"{s['where']}: empty")
        else:
            left = f"{s['remain']}% left" if s["remain"] >= 0 else "amount unknown (no RFID tag)"
            print(f"{s['where']}: {s['name']} {s['hex']} · {left}" + (" · printing with this now" if s["active"] else ""))


# ---------- the dashboard window ----------
def main_window():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, GLib, WebKit2

    GLib.set_prgname("clicky-ams")
    GLib.set_application_name("Clicky AMS")
    cfg = load_config()

    ucm = WebKit2.UserContentManager()
    ucm.register_script_message_handler("ams")
    view = WebKit2.WebView.new_with_user_content_manager(ucm)
    s = view.get_settings()
    s.set_enable_developer_extras(False)
    s.set_allow_file_access_from_file_urls(True)

    def send(msg):
        js = "window.fromApp && window.fromApp(" + json.dumps(msg) + ");"
        def run():
            try:
                view.evaluate_javascript(js, -1, None, None, None, None, None)
            except AttributeError:
                view.run_javascript(js, None, None, None)
            return False
        GLib.idle_add(run)

    def public_config():
        pr = dict(cfg.get("printer") or {})
        pr["hasCode"] = bool(pr.pop("code", ""))
        return {"type": "config", "printer": pr, "spools": cfg["spools"]}

    link = LiveLink(lambda state: send(dict(state, type="state")))

    def connect_saved():
        pr = cfg.get("printer") or {}
        link.start(pr.get("ip", ""), pr.get("serial", ""), pr.get("code", ""))

    def on_message(manager, result):
        try:
            try:
                raw = result.get_js_value().to_string()
            except AttributeError:
                raw = result.to_string()
            msg = json.loads(raw)
        except Exception:
            return
        kind = msg.get("type")
        if kind == "ready":
            send(public_config())
            connect_saved()
        elif kind == "savePrinter":
            pr = cfg.get("printer") or {}
            pr.update({k: str(msg.get(k, "")).strip() for k in ("ip", "serial", "name", "model")})
            if str(msg.get("code", "")).strip():
                pr["code"] = str(msg["code"]).strip()
            cfg["printer"] = pr
            save_config(cfg)
            send(public_config())
            connect_saved()
        elif kind == "forgetPrinter":
            link.stop()
            cfg.pop("printer", None)
            save_config(cfg)
            send(public_config())
            send({"type": "state", "connected": False, "connecting": False, "error": "", "hasData": False, "ams": [], "job": {}})
        elif kind == "saveSpools":
            cfg["spools"] = msg.get("spools") or []
            save_config(cfg)
        elif kind == "refresh":
            if link.status.get("connected"):
                link.pushall()
            else:
                connect_saved()
        elif kind == "find":
            def work():
                send({"type": "found", "printers": find_printers()})
            threading.Thread(target=work, daemon=True).start()

    ucm.connect("script-message-received::ams", on_message)

    win = Gtk.Window(title="Clicky AMS")
    win.set_default_size(1240, 860)
    icon = os.path.join(HERE, "icon.png")
    if os.path.exists(icon):
        try:
            win.set_icon_from_file(icon)
        except Exception:
            pass
    win.add(view)

    def quit_(*_):
        link.stop()
        Gtk.main_quit()
    win.connect("destroy", quit_)
    GLib.timeout_add_seconds(30, lambda: (link.tick(), True)[1])
    view.load_uri("file://" + os.path.join(HERE, "dashboard.html"))
    win.show_all()
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
