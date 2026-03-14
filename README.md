# BSB Control — Bigscreen Beyond GUI for Linux

System tray app for brightness, fan, and LED control with VRChat OSC integration.

## Install & Run

```bash
pip install PyQt6 python-osc --break-system-packages
chmod +x bsb_control.py
./bsb_control.py
```

Or build a standalone binary:
```bash
chmod +x build.sh
./build.sh
# → dist/bsb-control
```

## VRChat Setup

The app listens for OSC messages from VRChat avatar parameters:

### Radial Puppet (continuous brightness)
Add a radial puppet to your avatar's expression menu:
- **Parameter name**: `BSBBrightness` (Float, 0.0–1.0)
- The app maps 0→min% and 1→max% (configurable in UI)

### Bool Toggle (quick dim)
Add a toggle to your expression menu:
- **Parameter name**: `BSBDim` (Bool)
- ON = dim to configured level, OFF = restore to max

### OSC Config
Default port is **9001** (VRChat's default OSC output port). Adjust parameter names in the UI to match your avatar.

## Features

- System tray icon (click to show/hide, right-click for quick presets)
- Brightness slider with presets
- Fan speed control
- VRChat OSC listener with configurable parameter mapping
- Auto-reconnect when BSB is plugged/unplugged
- Config persists at `~/.config/bsb-control/config.json`
- Minimizes to tray on close

## udev Rules

Same as the CLI tool — needed for non-root access:
```bash
sudo tee /etc/udev/rules.d/99-bigscreen-beyond.rules > /dev/null << 'EOF'
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="35bd", ATTRS{idProduct}=="0101", MODE="0660", GROUP="wheel"
EOF
sudo udevadm control --reload && sudo udevadm trigger
```
Then replug BSB USB.
