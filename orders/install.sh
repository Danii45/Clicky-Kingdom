#!/bin/bash
# Installs Clicky Orders on Ubuntu: a small app window for the Clicky Kingdom order sheet.
# Run:  wget -qO- https://danii45.github.io/Clicky-Kingdom/orders/install.sh | bash
set -e
URL="https://danii45.github.io/Clicky-Kingdom/orders/"
DIR="$HOME/.local/share/clicky-orders"
echo "Installing Clicky Orders..."
echo "(You may be asked for your computer password, to install the window part.)"
sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 >/dev/null || echo "Couldn't install the window part; Clicky Orders will open in your web browser instead."
mkdir -p "$DIR" "$HOME/.local/share/applications"
wget -qO "$DIR/icon.png" "${URL}icon.png"
cat > "$DIR/clicky-orders.py" <<'PY'
#!/usr/bin/env python3
import os, subprocess, sys
URL = "https://danii45.github.io/Clicky-Kingdom/orders/"
try:
    import gi
    gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2, GLib
except Exception:
    subprocess.Popen(["xdg-open", URL]); sys.exit(0)
GLib.set_prgname("clicky-orders"); GLib.set_application_name("Clicky Orders")
base = os.path.expanduser("~/.local/share/clicky-orders/data")
dm = WebKit2.WebsiteDataManager(base_data_directory=base, base_cache_directory=os.path.expanduser("~/.cache/clicky-orders"))
ctx = WebKit2.WebContext.new_with_website_data_manager(dm)
view = WebKit2.WebView.new_with_context(ctx)
def allow(v, req):
    if isinstance(req, WebKit2.NotificationPermissionRequest): req.allow(); return True
    return False
view.connect("permission-request", allow)
def outside(v, decision, kind):  # links like "Open the sheet" go to the normal browser
    if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
        subprocess.Popen(["xdg-open", decision.get_navigation_action().get_request().get_uri()]); decision.ignore(); return True
    return False
view.connect("decide-policy", outside)
win = Gtk.Window(title="Clicky Orders"); win.set_default_size(1180, 820); win.set_wmclass("clicky-orders", "Clicky Orders")
try: win.set_icon_from_file(os.path.expanduser("~/.local/share/clicky-orders/icon.png"))
except Exception: pass
win.add(view); win.connect("destroy", Gtk.main_quit); win.show_all()
view.load_uri(URL)
Gtk.main()
PY
chmod +x "$DIR/clicky-orders.py"
cat > "$HOME/.local/share/applications/clicky-orders.desktop" <<EOF
[Desktop Entry]
Name=Clicky Orders
Comment=Clicky Kingdom orders
Exec=python3 $DIR/clicky-orders.py
Icon=$DIR/icon.png
Terminal=false
Type=Application
Categories=Office;
StartupWMClass=clicky-orders
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Done! Open \"Clicky Orders\" from your app menu."
(setsid python3 "$DIR/clicky-orders.py" >/dev/null 2>&1 &) || true
