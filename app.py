#!/usr/bin/env python3
from flask import Flask, request, render_template, redirect, jsonify
import dbus
import subprocess
import platform
import os
import secrets
import math
import time
import uuid

app = Flask(__name__)
CONNECT_ATTEMPTS = {}

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"

def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read().strip().replace("\x00", "")
    except Exception:
        return "Unknown"


def format_bytes(num_bytes):
    try:
        num_bytes = int(num_bytes)
    except Exception:
        return "Unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024

    return "Unknown"


def detect_device_model():
    model = read_file("/proc/device-tree/model")
    if model != "Unknown":
        return model

    return platform.machine()


def detect_cpu_arch():
    return platform.machine()


def detect_cpu_threads():
    count = os.cpu_count()
    return str(count) if count else "Unknown"


def detect_ram():
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        kb = int(parts[1])
                        return format_bytes(kb * 1024)
    except Exception:
        pass

    return "Unknown"


def hardware_info():
    return {
        "model": detect_device_model(),
        "cpu": detect_cpu_arch(),
        "cpu_threads": detect_cpu_threads(),
        "ram": detect_ram(),
        "hostname": read_file("/etc/hostname"),
    }
        
def bus():
    return dbus.SystemBus()

def nm_iface(path=NM_PATH, iface="org.freedesktop.NetworkManager"):
    obj = bus().get_object(NM, path)
    return dbus.Interface(obj, iface)

def get_wifi_device():
    nm = nm_iface()
    props = dbus.Interface(bus().get_object(NM, NM_PATH), "org.freedesktop.DBus.Properties")
    devices = props.Get(NM, "Devices")

    for dev in devices:
        dev_props = dbus.Interface(bus().get_object(NM, dev), "org.freedesktop.DBus.Properties")
        dev_type = dev_props.Get("org.freedesktop.NetworkManager.Device", "DeviceType")
        if int(dev_type) == 2:
            return dev
    return None


def saved_wifi_connections():
    settings = nm_iface(SETTINGS_PATH, "org.freedesktop.NetworkManager.Settings")
    saved = []

    for path in settings.ListConnections():
        conn = nm_iface(path, "org.freedesktop.NetworkManager.Settings.Connection")
        data = conn.GetSettings()

        if data.get("connection", {}).get("type") == "802-11-wireless":
            saved.append({
                "id": str(data["connection"].get("id", "")),
                "path": str(path),
            })

    return saved


def set_hostname(hostname):
    obj = bus().get_object(
        "org.freedesktop.hostname1",
        "/org/freedesktop/hostname1"
    )
    iface = dbus.Interface(obj, "org.freedesktop.hostname1")
    iface.SetStaticHostname(hostname, False)


def set_timezone(timezone):
    obj = bus().get_object(
        "org.freedesktop.timedate1",
        "/org/freedesktop/timedate1"
    )
    iface = dbus.Interface(obj, "org.freedesktop.timedate1")
    iface.SetTimezone(timezone, False)

def mark_setup_complete():
    os.makedirs("/etc/kioskos", exist_ok=True)

    with open("/etc/kioskos/setup-complete", "w") as f:
        f.write("complete\n")
        
def forget_connection(path):
    conn = nm_iface(path, "org.freedesktop.NetworkManager.Settings.Connection")
    conn.Delete()

def list_timezones():
    path = "/usr/share/zoneinfo/zone1970.tab"
    zones = []

    with open(path, "r") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = line.split("\t")

            if len(parts) >= 3:
                zones.append(parts[2])

    return sorted(set(zones))
  
def reboot_system():
    obj = bus().get_object(
        "org.freedesktop.login1",
        "/org/freedesktop/login1"
    )
    manager = dbus.Interface(
        obj,
        "org.freedesktop.login1.Manager"
    )
    manager.Reboot(False)

def scan_networks():
    dev = get_wifi_device()
    if not dev:
        return []

    wifi = nm_iface(dev, "org.freedesktop.NetworkManager.Device.Wireless")
    aps = wifi.GetAccessPoints()
    networks = []

    for ap in aps:
        props = dbus.Interface(bus().get_object(NM, ap), "org.freedesktop.DBus.Properties")
        ssid_bytes = props.Get("org.freedesktop.NetworkManager.AccessPoint", "Ssid")
        strength = int(props.Get("org.freedesktop.NetworkManager.AccessPoint", "Strength"))

        ssid = bytes(ssid_bytes).decode("utf-8", errors="ignore")
        if ssid:
            networks.append({"ssid": ssid, "strength": strength})

    seen = {}
    for n in networks:
        seen[n["ssid"]] = max(seen.get(n["ssid"], 0), n["strength"])

    return sorted(
        [{"ssid": k, "strength": v} for k, v in seen.items()],
        key=lambda x: x["strength"],
        reverse=True
    )

def set_saved_wifi_autoconnect(ssid, enabled=True):
    settings = nm_iface(SETTINGS_PATH, "org.freedesktop.NetworkManager.Settings")

    for path in settings.ListConnections():
        conn = nm_iface(path, "org.freedesktop.NetworkManager.Settings.Connection")
        data = conn.GetSettings()

        if data.get("connection", {}).get("type") != "802-11-wireless":
            continue

        saved_ssid_bytes = data.get("802-11-wireless", {}).get("ssid")
        if not saved_ssid_bytes:
            continue

        saved_ssid = bytes(saved_ssid_bytes).decode("utf-8", errors="ignore")

        if saved_ssid != ssid:
            continue

        data["connection"]["autoconnect"] = dbus.Boolean(enabled)
        data["connection"]["autoconnect-priority"] = dbus.Int32(100)

        conn.Update(data)
        return True

    return False

def delete_saved_wifi_by_ssid(ssid):
    settings = nm_iface(SETTINGS_PATH, "org.freedesktop.NetworkManager.Settings")

    for path in settings.ListConnections():
        conn = nm_iface(path, "org.freedesktop.NetworkManager.Settings.Connection")
        data = conn.GetSettings()

        if data.get("connection", {}).get("type") != "802-11-wireless":
            continue

        saved_ssid_bytes = data.get("802-11-wireless", {}).get("ssid")
        if not saved_ssid_bytes:
            continue

        saved_ssid = bytes(saved_ssid_bytes).decode("utf-8", errors="ignore")

        if saved_ssid == ssid:
            conn.Delete()
          
          
NM_DEVICE_STATE_DISCONNECTED = 30
NM_DEVICE_STATE_NEED_AUTH = 60
NM_DEVICE_STATE_ACTIVATED = 100
NM_DEVICE_STATE_FAILED = 120


def wifi_failure_message(dev_state=None, reason=None):
    return "Wi-Fi connection failed. Make sure the password is correct, the router is online, and the signal is strong, then try again."
          
          
def connect_wifi(ssid, password):
    dev = get_wifi_device()
    if not dev:
        raise RuntimeError("No Wi-Fi device found")

    if not ssid:
        raise RuntimeError("No Wi-Fi network selected")

    nm = nm_iface()

    try:
        dev_iface = dbus.Interface(
            bus().get_object(NM, dev),
            "org.freedesktop.NetworkManager.Device"
        )
        dev_iface.Disconnect()
    except Exception:
        pass

    s_con = dbus.Dictionary({
        "type": dbus.String("802-11-wireless"),
        "id": dbus.String(ssid),
        "uuid": dbus.String(str(uuid.uuid4())),
        "autoconnect": dbus.Boolean(False),
    }, signature="sv")

    s_wifi = dbus.Dictionary({
        "ssid": dbus.ByteArray(ssid.encode("utf-8")),
        "mode": dbus.String("infrastructure"),
    }, signature="sv")

    s_ip4 = dbus.Dictionary({
        "method": dbus.String("auto"),
    }, signature="sv")

    s_ip6 = dbus.Dictionary({
        "method": dbus.String("auto"),
    }, signature="sv")

    connection = dbus.Dictionary({
        "connection": s_con,
        "802-11-wireless": s_wifi,
        "ipv4": s_ip4,
        "ipv6": s_ip6,
    }, signature="sa{sv}")

    if password:
        s_wifi["security"] = dbus.String("802-11-wireless-security")

        s_wsec = dbus.Dictionary({
            "key-mgmt": dbus.String("wpa-psk"),
            "psk": dbus.String(password),
            "psk-flags": dbus.UInt32(0),
        }, signature="sv")

        connection["802-11-wireless-security"] = s_wsec

    delete_saved_wifi_by_ssid(ssid)

    connection_path, active_connection = nm.AddAndActivateConnection(
        connection,
        dbus.ObjectPath(dev),
        dbus.ObjectPath("/")
    )

    return str(active_connection)


def systemd_manager():
    obj = bus().get_object(
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1"
    )
    return dbus.Interface(obj, "org.freedesktop.systemd1.Manager")

def pretty_timezone_name(tz):
    return tz.replace("_", " ").replace("/", " / ")


def current_timezone():
    try:
        target = os.path.realpath("/etc/localtime")
        prefix = "/usr/share/zoneinfo/"

        if target.startswith(prefix):
            return target[len(prefix):]
    except Exception:
        pass

    return ""


def timezone_options():
    return [
        {
            "value": tz,
            "label": pretty_timezone_name(tz),
            "search": pretty_timezone_name(tz).lower(),
        }
        for tz in list_timezones()
    ]

def wifi_device_state_reason():
    dev = get_wifi_device()
    if not dev:
        return None, None

    props = dbus.Interface(
        bus().get_object(NM, dev),
        "org.freedesktop.DBus.Properties"
    )

    state = int(props.Get(
        "org.freedesktop.NetworkManager.Device",
        "State"
    ))

    reason = props.Get(
        "org.freedesktop.NetworkManager.Device",
        "StateReason"
    )

    try:
        reason_code = int(reason[1])
    except Exception:
        reason_code = None

    return state, reason_code

def ssh_is_enabled():
    try:
        manager = systemd_manager()
        unit_file_state = manager.GetUnitFileState("ssh.service")
        return str(unit_file_state) == "enabled"
    except Exception:
        return False
        
def set_ssh_enabled(enabled):
    manager = systemd_manager()
    unit = "ssh.service"

    if enabled:
        manager.EnableUnitFiles([unit], False, True)
        manager.StartUnit(unit, "replace")
    else:
        manager.StopUnit(unit, "replace")
        manager.DisableUnitFiles([unit], False)

@app.route("/")
@app.route("/welcome")
def welcome():
    return render_template("welcome.html", info=hardware_info())

@app.route("/device", methods=["GET", "POST"])
def device():
    if request.method == "POST":
        hostname = request.form.get("hostname", "").strip()

        if not hostname:
            return render_template(
                "device.html",
                hostname=read_file("/etc/hostname"),
                error="Device name cannot be empty."
            )

        try:
            set_hostname(hostname)
            return redirect("/timezone")
        except Exception as e:
            return render_template(
                "device.html",
                hostname=read_file("/etc/hostname"),
                error=str(e)
            )

    return render_template(
        "device.html",
        hostname=read_file("/etc/hostname"),
        error=None
    )
    
@app.route("/scan-wifi")
def scan_wifi_route():
    try:
        updated = request_wifi_scan()
        networks = scan_networks()

        return jsonify({
            "status": "ok",
            "updated": updated,
            "networks": networks,
            "error": None
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "updated": False,
            "networks": [],
            "error": str(e)
        })
        
@app.route("/network")
def network_choice():
    return render_template("network.html", error=None)


@app.route("/network/ethernet", methods=["POST"])
def network_ethernet():
    try:
        set_wifi_radio_enabled(False)
    except Exception:
        pass

    return redirect("/ssh")


@app.route("/network/wifi", methods=["POST"])
def network_wifi():
    try:
        set_wifi_radio_enabled(True)
    except Exception:
        pass

    return redirect("/wifi?scan=1")


@app.route("/wifi")
def index():
    try:
        wifi_enabled = wifi_radio_enabled()

        networks = scan_networks() if wifi_enabled else []
        saved = saved_wifi_connections()
        error = None
    except Exception as e:
        wifi_enabled = False
        networks = []
        saved = []
        error = str(e)

    return render_template(
        "wifi.html",
        networks=networks,
        saved=saved,
        wifi_enabled=wifi_enabled,
        error=error
    )

@app.route("/forget", methods=["POST"])
def forget():
    path = request.form.get("path", "")

    try:
        forget_connection(path)
        wifi_enabled = wifi_radio_enabled()

        return render_template(
            "wifi.html",
            networks=scan_networks() if wifi_enabled else [],
            saved=saved_wifi_connections(),
            wifi_enabled=wifi_enabled,
            error="Network forgotten."
        )
    except Exception as e:
        wifi_enabled = wifi_radio_enabled()

        return render_template(
            "wifi.html",
            networks=scan_networks() if wifi_enabled else [],
            saved=saved_wifi_connections(),
            wifi_enabled=wifi_enabled,
            error=str(e)
        )
        
@app.route("/saved-networks-wifi")
def saved_page():
    try:
        saved = saved_wifi_connections()
        error = None
    except Exception as e:
        saved = []
        error = str(e)

    return render_template("saved-networks-wifi.html", saved=saved, error=error)
    
@app.route("/connect", methods=["POST"])
def connect():
    ssid = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")

    try:
        active_path = connect_wifi(ssid, password)

        CONNECT_ATTEMPTS[ssid] = {
            "time": time.time(),
            "active_path": active_path,
        }

        return jsonify({
            "status": "connecting",
            "message": "Connecting..."
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        })

def generate_ssh_passphrase():
    words_path = os.path.join(
        os.path.dirname(__file__),
        "data",
        "words.txt"
    )

    with open(words_path, "r") as f:
        words = [
            line.strip()
            for line in f
            if line.strip().isalpha()
        ]

    parts = []

    for _ in range(4):
        number = secrets.randbelow(10)
        word = secrets.choice(words)
        parts.append(f"{number}{word}")

    return "-".join(parts)


@app.route("/generate-ssh-password")
def generate_ssh_password():
    return jsonify({
        "password": generate_ssh_passphrase()
    })

def active_connection_state(active_path):
    props = dbus.Interface(
        bus().get_object(NM, active_path),
        "org.freedesktop.DBus.Properties"
    )

    return int(props.Get(
        "org.freedesktop.NetworkManager.Connection.Active",
        "State"
    ))
    
def active_wifi_ssid():
    dev = get_wifi_device()
    if not dev:
        return None

    props = dbus.Interface(
        bus().get_object(NM, dev),
        "org.freedesktop.DBus.Properties"
    )

    ap_path = props.Get(
        "org.freedesktop.NetworkManager.Device.Wireless",
        "ActiveAccessPoint"
    )

    if str(ap_path) == "/":
        return None

    ap_props = dbus.Interface(
        bus().get_object(NM, ap_path),
        "org.freedesktop.DBus.Properties"
    )

    ssid_bytes = ap_props.Get(
        "org.freedesktop.NetworkManager.AccessPoint",
        "Ssid"
    )

    return bytes(ssid_bytes).decode("utf-8", errors="ignore")
    
@app.route("/status")
def status():
    ssid = request.args.get("ssid", "").strip()

    attempt = CONNECT_ATTEMPTS.get(ssid)

    if not attempt:
        return jsonify({
            "status": "connecting",
            "message": "Connecting..."
        })

    active_path = attempt["active_path"]
    since = attempt["time"]
    elapsed = time.time() - since

    def fail(message=None):
        CONNECT_ATTEMPTS.pop(ssid, None)

        try:
            delete_saved_wifi_by_ssid(ssid)
        except Exception:
            pass

        return jsonify({
            "status": "error",
            "message": message or "Could not connect to this network. Check the password, router, and signal, then try again."
        })

    try:
        state = active_connection_state(active_path)

        if state == 2 and active_wifi_ssid() == ssid:
            CONNECT_ATTEMPTS.pop(ssid, None)

            try:
                set_saved_wifi_autoconnect(ssid, True)
            except Exception:
                pass

            return jsonify({
                "status": "connected",
                "message": "Connected successfully."
            })

        if state in (3, 4) and elapsed > 2:
            try:
                dev_state, reason = wifi_device_state_reason()
            except Exception:
                dev_state, reason = None, None

            return fail(wifi_failure_message(dev_state, reason))

    except Exception:
        if elapsed > 3:
            try:
                dev_state, reason = wifi_device_state_reason()
            except Exception:
                dev_state, reason = None, None

            return fail(wifi_failure_message(dev_state, reason))

    try:
        dev_state, reason = wifi_device_state_reason()

        if dev_state == NM_DEVICE_STATE_NEED_AUTH and elapsed > 2:
            return fail(wifi_failure_message(dev_state, reason))

        if dev_state in (NM_DEVICE_STATE_DISCONNECTED, NM_DEVICE_STATE_FAILED) and elapsed > 3:
            return fail(wifi_failure_message(dev_state, reason))

    except Exception:
        pass

    if elapsed > 20:
        return fail(wifi_failure_message(None, None))

    return jsonify({
        "status": "connecting",
        "message": "Connecting..."
    })

@app.route("/timezone", methods=["GET", "POST"])
def timezone():
    options = timezone_options()
    selected_timezone = current_timezone()

    if request.method == "POST":
        tz = request.form.get("timezone", "").strip()

        try:
            set_timezone(tz)
            return redirect("/network")
        except Exception as e:
            return render_template(
                "timezone.html",
                timezones=options,
                selected_timezone=selected_timezone,
                error=str(e)
            )

    return render_template(
        "timezone.html",
        timezones=options,
        selected_timezone=selected_timezone,
        error=None
    )
   
def set_user_password(username, password):
    subprocess.run(
        ["sudo", "/usr/local/sbin/kioskos-set-password", username],
        input=password + "\n",
        text=True,
        check=True,
        capture_output=True
    )


@app.route("/ssh", methods=["GET", "POST"])
def ssh_setup():
    if request.method == "POST":
        enabled = request.form.get("enable_ssh") == "on"
        password = request.form.get("ssh_password", "")
        accept_weak = request.form.get("accept_weak_password") == "on"

        try:
            if enabled:
                if not ssh_is_enabled() and not password:
                    return render_template(
                        "ssh.html",
                        ssh_enabled=ssh_is_enabled(),
                        weak_password=False,
                        error="Choose a password or generate one to enable SSH."
                    )

                if password:
                    strength = password_strength(password)

                    if strength["weak"] and not accept_weak:
                        return render_template(
                            "ssh.html",
                            ssh_enabled=True,
                            weak_password=True,
                            ssh_password=password,
                            entropy=strength["entropy"],
                            error=f"This password is estimated at {strength['entropy']} bits. You can accept the risk or choose a stronger password."
                        )

                    set_user_password("kiosk", password)

                set_ssh_enabled(True)
            else:
                set_ssh_enabled(False)

            return redirect("/finish")

        except Exception as e:
            return render_template(
                "ssh.html",
                ssh_enabled=ssh_is_enabled(),
                weak_password=False,
                error=str(e)
            )

    return render_template(
        "ssh.html",
        ssh_enabled=ssh_is_enabled(),
        weak_password=False,
        error=None
    )

@app.route("/wifi-radio", methods=["POST"])
def wifi_radio():
    enabled = request.form.get("enabled") == "true"

    try:
        set_wifi_radio_enabled(enabled)

        scan_updated = False
        networks = []

        if enabled:
            try:
                scan_updated = request_wifi_scan(timeout=8)
                networks = scan_networks()
            except Exception:
                pass

        return jsonify({
            "status": "ok",
            "enabled": wifi_radio_enabled(),
            "scan_updated": scan_updated,
            "networks": networks,
            "error": None
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "enabled": wifi_radio_enabled(),
            "error": str(e)
        })
        
def wifi_radio_enabled():
    props = dbus.Interface(
        bus().get_object(NM, NM_PATH),
        "org.freedesktop.DBus.Properties"
    )
    return bool(props.Get(NM, "WirelessEnabled"))


def set_wifi_radio_enabled(enabled):
    props = dbus.Interface(
        bus().get_object(NM, NM_PATH),
        "org.freedesktop.DBus.Properties"
    )
    props.Set(NM, "WirelessEnabled", dbus.Boolean(enabled))
    
def request_wifi_scan(timeout=8):
    dev = get_wifi_device()
    if not dev:
        raise RuntimeError("No Wi-Fi device found")

    wifi = nm_iface(dev, "org.freedesktop.NetworkManager.Device.Wireless")
    props = dbus.Interface(
        bus().get_object(NM, dev),
        "org.freedesktop.DBus.Properties"
    )

    before = int(props.Get(
        "org.freedesktop.NetworkManager.Device.Wireless",
        "LastScan"
    ))

    try:
        wifi.RequestScan({})
    except Exception:
        return False

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        current = int(props.Get(
            "org.freedesktop.NetworkManager.Device.Wireless",
            "LastScan"
        ))

        if current != before and current > 0:
            return True

        time.sleep(0.2)

    return False
    
def password_entropy_bits(password):
    if not password:
        return 0

    pool = 0

    if any(c.islower() for c in password):
        pool += 26

    if any(c.isupper() for c in password):
        pool += 26

    if any(c.isdigit() for c in password):
        pool += 10

    if any(not c.isalnum() for c in password):
        pool += 32

    if pool == 0:
        return 0

    return round(len(password) * math.log2(pool))


def password_strength(password):
    entropy = password_entropy_bits(password)

    return {
        "weak": entropy < 50,
        "entropy": entropy,
    }

@app.route("/finish", methods=["GET", "POST"])
def finish():
    if request.method == "POST":
        try:
            mark_setup_complete()
            return render_template("finish.html", complete=True, error=None)
        except Exception as e:
            return render_template("finish.html", complete=False, error=str(e))

    return render_template("finish.html", complete=False, error=None)


@app.route("/reboot", methods=["POST"])
def reboot():
    try:
        reboot_system()
        return "Rebooting..."
    except Exception as e:
        return render_template("finish.html", complete=True, error=str(e))
    
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
