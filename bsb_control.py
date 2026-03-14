#!/usr/bin/env python3
"""
BSB Control — Bigscreen Beyond brightness/fan/LED control with VRChat OSC.

System tray app for Linux. Controls BSB hardware via HID feature reports,
with optional VRChat OSC parameter mapping for in-VR control.

Protocol from OyasumiVR (MIT) — github.com/Raphiiko/OyasumiVR
"""

import sys
import os
import glob
import fcntl
import select
import signal
import json
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QSystemTrayIcon, QMenu, QPushButton,
    QLineEdit, QSpinBox, QGroupBox, QFrame, QCheckBox,
    QGraphicsDropShadowEffect, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QSize
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QAction,
    QLinearGradient, QPen, QBrush
)

# ─── BSB HID Protocol ────────────────────────────────────────────────────────

BSB_VID = 0x35BD
BSB_PID = 0x0101

CMD_BRIGHTNESS = 0x49  # [0x00, 0x49, hi, lo] — u16 BE, 0-1023
CMD_FAN_SPEED  = 0x46  # [0x00, 0x46, speed]  — u8, 0-100
CMD_LED_COLOR  = 0x4C  # [0x00, 0x4c, R, G, B]


def find_bsb():
    vid_hex = f"{BSB_VID:08X}"
    pid_hex = f"{BSB_PID:08X}"
    for uevent_path in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(uevent_path) as f:
                content = f.read()
        except PermissionError:
            continue
        for line in content.splitlines():
            if line.startswith("HID_ID="):
                parts = line.split("=", 1)[1].split(":")
                if len(parts) >= 3 and vid_hex in parts[1].upper() and pid_hex in parts[2].upper():
                    name = os.path.basename(uevent_path.split("/device/")[0])
                    return f"/dev/{name}"
    return None


def _hid_ioctl(nr, size):
    return (3 << 30) | (size << 16) | (0x48 << 8) | nr


class BSBDevice:
    def __init__(self):
        self._fd = None
        self.path = None
        self.connected = False

    def try_connect(self):
        path = find_bsb()
        if not path:
            self.connected = False
            return False
        if self._fd is not None and self.path == path:
            self.connected = True
            return True
        self.disconnect()
        try:
            self._fd = os.open(path, os.O_RDWR)
            self.path = path
            self.connected = True
            return True
        except (PermissionError, FileNotFoundError):
            self.connected = False
            return False

    def disconnect(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self.connected = False

    def _send_feature(self, *payload):
        if self._fd is None:
            return False
        buf = bytearray(65)
        buf[0] = 0x00
        for i, b in enumerate(payload):
            buf[i + 1] = b
        try:
            fcntl.ioctl(self._fd, _hid_ioctl(0x06, len(buf)), buf)
            return True
        except OSError:
            self.connected = False
            return False

    def set_brightness(self, pct):
        val = max(0, min(1023, int(pct / 100.0 * 1023)))
        return self._send_feature(CMD_BRIGHTNESS, (val >> 8) & 0xFF, val & 0xFF)

    def set_fan(self, pct):
        val = max(0, min(100, int(pct)))
        return self._send_feature(CMD_FAN_SPEED, val)

    def set_led(self, r, g, b):
        return self._send_feature(CMD_LED_COLOR, r & 0xFF, g & 0xFF, b & 0xFF)


# ─── OSC Listener ────────────────────────────────────────────────────────────

class OSCListener:
    def __init__(self):
        self._server = None
        self._thread = None
        self.running = False
        self.on_brightness = None  # callback(float 0-1)
        self.on_toggle = None      # callback(bool)
        self.brightness_param = "/avatar/parameters/BSBBrightness"
        self.toggle_param = "/avatar/parameters/BSBDim"
        self.port = 9001

    def start(self):
        if self.running:
            return
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
        except ImportError:
            return False

        dispatcher = Dispatcher()
        dispatcher.map(self.brightness_param, self._on_brightness_msg)
        dispatcher.map(self.toggle_param, self._on_toggle_msg)

        try:
            self._server = ThreadingOSCUDPServer(("127.0.0.1", self.port), dispatcher)
        except OSError as e:
            print(f"OSC bind failed: {e}")
            return False

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.running = True
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
        self._thread = None
        self.running = False

    def _on_brightness_msg(self, addr, *args):
        if args and self.on_brightness:
            val = float(args[0])
            self.on_brightness(max(0.0, min(1.0, val)))

    def _on_toggle_msg(self, addr, *args):
        if args and self.on_toggle:
            self.on_toggle(bool(args[0]))


# ─── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".config" / "bsb-control" / "config.json"

DEFAULT_CONFIG = {
    "osc_port": 9001,
    "brightness_param": "/avatar/parameters/BSBBrightness",
    "toggle_param": "/avatar/parameters/BSBDim",
    "brightness_min": 10,
    "brightness_max": 30,
    "dim_brightness": 15,
    "dim_fan": 70,
    "osc_autostart": False,
    "startup_brightness": 30,
    "startup_fan": 80,
    "last_brightness": 30,
    "last_fan": 80,
}

def load_config():
    try:
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ─── Tray Icon ───────────────────────────────────────────────────────────────

def make_icon(connected=False, brightness=100):
    """Generate a 64x64 tray icon — sun/brightness symbol."""
    px = QPixmap(64, 64)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    color = QColor(230, 190, 60) if connected else QColor(120, 120, 120)
    alpha = int(80 + (brightness / 100.0) * 175)
    color.setAlpha(min(255, alpha))

    # Sun circle
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawEllipse(18, 18, 28, 28)

    # Rays
    pen = QPen(color, 3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    import math
    for i in range(8):
        angle = i * math.pi / 4
        x1 = 32 + math.cos(angle) * 20
        y1 = 32 + math.sin(angle) * 20
        x2 = 32 + math.cos(angle) * 28
        y2 = 32 + math.sin(angle) * 28
        p.drawLine(int(x1), int(y1), int(x2), int(y2))

    p.end()
    return QIcon(px)


# ─── Styled Widgets ──────────────────────────────────────────────────────────

STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Segoe UI', 'Noto Sans', sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #2a2a4a;
    border-radius: 10px;
    margin-top: 14px;
    padding: 18px 14px 14px 14px;
    background-color: #16162a;
    font-weight: bold;
    font-size: 12px;
    color: #8888aa;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 16px;
    padding: 0 6px;
    color: #8888aa;
}
QSlider::groove:horizontal {
    height: 8px;
    background: #2a2a4a;
    border-radius: 4px;
}
QSlider::handle:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #7c5cbf, stop:1 #5a3d9e);
    width: 20px;
    height: 20px;
    margin: -7px 0;
    border-radius: 10px;
    border: 2px solid #9b7dd4;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #5a3d9e, stop:1 #7c5cbf);
    border-radius: 4px;
}
QPushButton {
    background-color: #2a2a4a;
    border: 1px solid #3a3a5a;
    border-radius: 6px;
    padding: 6px 16px;
    color: #d0d0e0;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #3a3a5a;
    border-color: #7c5cbf;
}
QPushButton:pressed {
    background-color: #5a3d9e;
}
QPushButton:checked {
    background-color: #5a3d9e;
    border-color: #9b7dd4;
    color: white;
}
QLineEdit, QSpinBox {
    background-color: #12121f;
    border: 1px solid #2a2a4a;
    border-radius: 4px;
    padding: 5px 8px;
    color: #d0d0e0;
    font-size: 13px;
    selection-background-color: #5a3d9e;
}
QLineEdit:focus, QSpinBox:focus {
    border-color: #7c5cbf;
}
QLabel {
    font-size: 13px;
}
QCheckBox {
    spacing: 6px;
    font-size: 13px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 3px;
    border: 1px solid #3a3a5a;
    background: #12121f;
}
QCheckBox::indicator:checked {
    background: #5a3d9e;
    border-color: #9b7dd4;
}
"""


class StatusDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._connected = False

    def set_connected(self, c):
        self._connected = c
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(80, 220, 100) if self._connected else QColor(220, 60, 60)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(1, 1, 8, 8)
        p.end()


# ─── Main Window ─────────────────────────────────────────────────────────────

class SignalBridge(QObject):
    brightness_changed = pyqtSignal(float)
    toggle_changed = pyqtSignal(bool)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BSB Control")
        self.setFixedSize(460, 590)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self.cfg = load_config()
        self.bsb = BSBDevice()
        self.osc = OSCListener()
        self.signals = SignalBridge()
        self.current_brightness = self.cfg.get("last_brightness", self.cfg["startup_brightness"])
        self.current_fan = self.cfg.get("last_fan", self.cfg["startup_fan"])
        self._dim_active = False
        self._first_connect = True
        self._pre_dim_brightness = self.current_brightness
        self._pre_dim_fan = self.current_fan

        self._build_ui()
        self._setup_tray()
        self._setup_osc()
        self._setup_timers()

        self.signals.brightness_changed.connect(self._on_osc_brightness)
        self.signals.toggle_changed.connect(self._on_osc_toggle)

        # Initial connection attempt
        self._poll_device()

        if self.cfg.get("osc_autostart", False):
            self._toggle_osc()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        # ── Header ──
        header = QHBoxLayout()
        title = QLabel("BSB Control")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #9b7dd4;")
        header.addWidget(title)
        header.addStretch()
        self.status_dot = StatusDot()
        header.addWidget(self.status_dot)
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("font-size: 12px; color: #888;")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        # ── Brightness ──
        bri_group = QGroupBox("BRIGHTNESS")
        bri_layout = QVBoxLayout(bri_group)

        slider_row = QHBoxLayout()
        self.bri_slider = QSlider(Qt.Orientation.Horizontal)
        self.bri_slider.setRange(0, 100)
        self.bri_slider.setValue(int(self.current_brightness))
        self.bri_slider.valueChanged.connect(self._on_slider_change)
        slider_row.addWidget(self.bri_slider)
        self.bri_label = QLabel(f"{int(self.current_brightness)}%")
        self.bri_label.setFixedWidth(52)
        self.bri_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bri_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #9b7dd4;")
        slider_row.addWidget(self.bri_label)
        bri_layout.addLayout(slider_row)

        presets = QHBoxLayout()
        for pct in [10, 25, 30, 50, 80, 100]:
            btn = QPushButton(f"{pct}")
            btn.setFixedHeight(32)
            btn.setStyleSheet("font-size: 13px;")
            btn.clicked.connect(lambda _, v=pct: self._set_brightness(v))
            presets.addWidget(btn)
        bri_layout.addLayout(presets)
        layout.addWidget(bri_group)

        # ── Fan ──
        fan_group = QGroupBox("FAN SPEED")
        fan_layout = QHBoxLayout(fan_group)
        self.fan_slider = QSlider(Qt.Orientation.Horizontal)
        self.fan_slider.setRange(0, 100)
        self.fan_slider.setValue(int(self.current_fan))
        self.fan_slider.valueChanged.connect(self._on_fan_change)
        fan_layout.addWidget(self.fan_slider)
        self.fan_label = QLabel(f"{int(self.current_fan)}%")
        self.fan_label.setFixedWidth(52)
        self.fan_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.fan_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #9b7dd4;")
        fan_layout.addWidget(self.fan_label)
        layout.addWidget(fan_group)

        # ── OSC ──
        osc_group = QGroupBox("VRCHAT OSC")
        osc_layout = QVBoxLayout(osc_group)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(self.cfg["osc_port"])
        port_row.addWidget(self.port_spin)
        osc_layout.addLayout(port_row)

        param_row = QHBoxLayout()
        param_row.addWidget(QLabel("Brightness"))
        self.bri_param_edit = QLineEdit(self.cfg["brightness_param"])
        self.bri_param_edit.setPlaceholderText("/avatar/parameters/BSBBrightness")
        param_row.addWidget(self.bri_param_edit)
        osc_layout.addLayout(param_row)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(QLabel("Dim toggle"))
        self.toggle_param_edit = QLineEdit(self.cfg["toggle_param"])
        self.toggle_param_edit.setPlaceholderText("/avatar/parameters/BSBDim")
        toggle_row.addWidget(self.toggle_param_edit)
        osc_layout.addLayout(toggle_row)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Range"))
        self.min_spin = QSpinBox()
        self.min_spin.setRange(0, 100)
        self.min_spin.setValue(self.cfg["brightness_min"])
        self.min_spin.setSuffix("%")
        range_row.addWidget(self.min_spin)
        range_row.addWidget(QLabel("–"))
        self.max_spin = QSpinBox()
        self.max_spin.setRange(0, 100)
        self.max_spin.setValue(self.cfg["brightness_max"])
        self.max_spin.setSuffix("%")
        range_row.addWidget(self.max_spin)
        osc_layout.addLayout(range_row)

        dim_row = QHBoxLayout()
        dim_row.addWidget(QLabel("Dim to"))
        self.dim_spin = QSpinBox()
        self.dim_spin.setRange(0, 100)
        self.dim_spin.setValue(self.cfg["dim_brightness"])
        self.dim_spin.setSuffix("%")
        dim_row.addWidget(self.dim_spin)
        dim_row.addWidget(QLabel("brightness"))
        dim_row.addStretch()
        self.dim_fan_spin = QSpinBox()
        self.dim_fan_spin.setRange(0, 100)
        self.dim_fan_spin.setValue(self.cfg.get("dim_fan", 70))
        self.dim_fan_spin.setSuffix("%")
        dim_row.addWidget(self.dim_fan_spin)
        dim_row.addWidget(QLabel("fan"))
        osc_layout.addLayout(dim_row)

        btn_row = QHBoxLayout()
        self.osc_btn = QPushButton("Start OSC")
        self.osc_btn.setCheckable(True)
        self.osc_btn.setMinimumWidth(100)
        self.osc_btn.setFixedHeight(32)
        self.osc_btn.clicked.connect(self._toggle_osc)
        btn_row.addWidget(self.osc_btn)
        self.autostart_cb = QCheckBox("Auto-start")
        self.autostart_cb.setChecked(self.cfg.get("osc_autostart", False))
        self.autostart_cb.toggled.connect(self._on_autostart_toggle)
        btn_row.addWidget(self.autostart_cb)
        btn_row.addStretch()
        self.osc_status = QLabel("")
        self.osc_status.setStyleSheet("font-size: 12px; color: #888;")
        btn_row.addWidget(self.osc_status)
        osc_layout.addLayout(btn_row)

        layout.addWidget(osc_group)
        layout.addStretch()

        # ── Footer ──
        footer = QLabel("Protocol: OyasumiVR (MIT)")
        footer.setStyleSheet("font-size: 11px; color: #444;")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(footer)

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(make_icon(False, 100))
        self.tray.setToolTip("BSB Control")

        menu = QMenu()
        self.tray_brightness_label = QAction(f"Brightness: {int(self.current_brightness)}%", self)
        self.tray_brightness_label.setEnabled(False)
        menu.addAction(self.tray_brightness_label)
        menu.addSeparator()

        for pct in [100, 80, 50, 30, 10]:
            a = QAction(f"Set {pct}%", self)
            a.triggered.connect(lambda _, v=pct: self._set_brightness(v))
            menu.addAction(a)
        menu.addSeparator()

        show_action = QAction("Show/Hide", self)
        show_action.triggered.connect(self._toggle_window)
        menu.addAction(show_action)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _setup_osc(self):
        self.osc.port = self.cfg["osc_port"]
        self.osc.brightness_param = self.cfg["brightness_param"]
        self.osc.toggle_param = self.cfg["toggle_param"]
        self.osc.on_brightness = lambda v: self.signals.brightness_changed.emit(v)
        self.osc.on_toggle = lambda v: self.signals.toggle_changed.emit(v)

    def _setup_timers(self):
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_device)
        self.poll_timer.start(3000)

    # ── Actions ──

    def _set_brightness(self, pct):
        self.current_brightness = pct
        self.cfg["last_brightness"] = pct
        self.bri_slider.blockSignals(True)
        self.bri_slider.setValue(int(pct))
        self.bri_slider.blockSignals(False)
        self.bri_label.setText(f"{pct:.0f}%")
        self.tray_brightness_label.setText(f"Brightness: {pct:.0f}%")
        self.tray.setIcon(make_icon(self.bsb.connected, pct))
        if self.bsb.connected:
            self.bsb.set_brightness(pct)

    def _on_slider_change(self, val):
        self._set_brightness(val)

    def _on_fan_change(self, val):
        self._set_fan(val)

    def _set_fan(self, pct):
        pct = int(max(0, min(100, pct)))
        self.current_fan = pct
        self.cfg["last_fan"] = pct
        self.fan_slider.blockSignals(True)
        self.fan_slider.setValue(pct)
        self.fan_slider.blockSignals(False)
        self.fan_label.setText(f"{pct}%")
        if self.bsb.connected:
            self.bsb.set_fan(pct)

    def _on_osc_brightness(self, val_01):
        lo = self.min_spin.value()
        hi = self.max_spin.value()
        pct = lo + val_01 * (hi - lo)
        self._set_brightness(pct)

    def _on_osc_toggle(self, active):
        self._dim_active = active
        if active:
            # Store current values to restore later
            self._pre_dim_brightness = self.current_brightness
            self._pre_dim_fan = self.current_fan
            self._set_brightness(self.dim_spin.value())
            self._set_fan(self.dim_fan_spin.value())
        else:
            self._set_brightness(getattr(self, '_pre_dim_brightness', self.max_spin.value()))
            self._set_fan(getattr(self, '_pre_dim_fan', self.cfg["startup_fan"]))

    def _toggle_osc(self):
        if self.osc.running:
            self.osc.stop()
            self.osc_btn.setChecked(False)
            self.osc_btn.setText("Start OSC")
            self.osc_status.setText("")
        else:
            self.osc.port = self.port_spin.value()
            self.osc.brightness_param = self.bri_param_edit.text()
            self.osc.toggle_param = self.toggle_param_edit.text()
            if self.osc.start():
                self.osc_btn.setChecked(True)
                self.osc_btn.setText("Stop OSC")
                self.osc_status.setText(f"Listening :{self.osc.port}")
                self._save_config()
            else:
                self.osc_btn.setChecked(False)
                self.osc_status.setText("Failed (python-osc?)")

    def _on_autostart_toggle(self, checked):
        self.cfg["osc_autostart"] = checked
        self._save_config()

    def _save_config(self):
        self.cfg.update({
            "osc_port": self.port_spin.value(),
            "brightness_param": self.bri_param_edit.text(),
            "toggle_param": self.toggle_param_edit.text(),
            "brightness_min": self.min_spin.value(),
            "brightness_max": self.max_spin.value(),
            "dim_brightness": self.dim_spin.value(),
            "dim_fan": self.dim_fan_spin.value(),
            "osc_autostart": self.autostart_cb.isChecked(),
            "last_brightness": self.current_brightness,
            "last_fan": self.current_fan,
        })
        save_config(self.cfg)

    def _poll_device(self):
        was = self.bsb.connected
        self.bsb.try_connect()
        if self.bsb.connected and not was:
            # Just connected — apply current values
            self.bsb.set_brightness(self.current_brightness)
            self.bsb.set_fan(self.current_fan)
        if self.bsb.connected != was:
            self.status_dot.set_connected(self.bsb.connected)
            self.status_label.setText(
                self.bsb.path.split("/")[-1] if self.bsb.connected else "Disconnected"
            )
            self.tray.setIcon(make_icon(self.bsb.connected, self.current_brightness))
            self.tray.setToolTip(
                f"BSB Control — {'Connected' if self.bsb.connected else 'Disconnected'}"
            )

    def _toggle_window(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _quit(self):
        self._save_config()
        self.osc.stop()
        self.bsb.disconnect()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C works

    app = QApplication(sys.argv)
    app.setApplicationName("BSB Control")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLESHEET)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("System tray not available")
        sys.exit(1)

    win = MainWindow()
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
