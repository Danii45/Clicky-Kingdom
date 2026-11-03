#!/bin/bash
# Installs Clicky AMS on Kubuntu/Ubuntu: shows what's loaded in your Bambu Lab AMS, over your home Wi-Fi.
# Run:  wget -O- https://danii45.github.io/Clicky-Kingdom/ams/install.sh | bash
set -e
URL="https://danii45.github.io/Clicky-Kingdom/ams/"
DIR="$HOME/.local/share/clicky-ams"
echo "Installing Clicky AMS..."
echo "(You may be asked for your computer password, to install the parts it needs.)"
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-paho-mqtt >/dev/null
mkdir -p "$DIR" "$HOME/.local/bin" "$HOME/.local/share/applications"
wget -qO "$DIR/clicky-ams.py" "${URL}clicky-ams.py"
wget -qO "$DIR/icon.png" "${URL}icon.png"
chmod +x "$DIR/clicky-ams.py"
ln -sf "$DIR/clicky-ams.py" "$HOME/.local/bin/clicky-ams"
cat > "$HOME/.local/share/applications/clicky-ams.desktop" <<EOF
[Desktop Entry]
Name=Clicky AMS
Comment=What's loaded in the Bambu Lab AMS
Exec=/usr/bin/python3 $DIR/clicky-ams.py
Icon=$DIR/icon.png
Terminal=false
Type=Application
Categories=Utility;
StartupWMClass=clicky-ams
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Done! Open \"Clicky AMS\" from your app menu."
(setsid /usr/bin/python3 "$DIR/clicky-ams.py" >/dev/null 2>&1 &) || true
