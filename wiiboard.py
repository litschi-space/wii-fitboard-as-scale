#!/usr/bin/env python3
"""
Wii Balance Board Reader - reines Python 3, keine externen Bluetooth-Libs.

Nutzt die eingebaute AF_BLUETOOTH/L2CAP-Unterstuetzung des socket-Moduls
(nur Linux, benoetigt den bluez-Systemdienst - der ist auf jedem
Raspberry Pi OS bereits vorinstalliert). Kein cwiid, kein xwiimote,
kein PyBluez.

Protokoll basiert auf skorokithakis/gr8w8upd8m8 (LGPL) und den bei
wiibrew.org dokumentierten Wiimote/Balance-Board HID-Reports.

Zusaetzlich: Veroeffentlicht jede Messung per MQTT, inkl. Home-Assistant
MQTT-Discovery-Config, sodass automatisch ein Sensor "Wii Balance Board
Gewicht" in HA auftaucht.

Benutzung:
    1. MAC-Adresse des Boards ermitteln, z.B. mit:
         bluetoothctl
         > scan on
         (roten Sync-Knopf im Batteriefach druecken, Geraet erscheint
          als "Nintendo RVL-WBC-01")
         > scan off

    2. Einmalig mit gedruecktem rotem Sync-Knopf verbinden, damit das
       Board die Pi-Adresse dauerhaft speichert (danach reicht der
       vordere Power-Knopf):
         sudo python3 wiiboard.py AA:BB:CC:DD:EE:FF --mqtt-host 192.168.1.10

    3. MQTT-Zugangsdaten per Argument oder Umgebungsvariablen setzen:
         MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD

Abhaengigkeit fuer MQTT:
    pip3 install paho-mqtt --break-system-packages
"""

import argparse
import collections
import datetime
import os
import socket
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

try:
    from escpos.printer import Usb
except ImportError:
    Usb = None

# Bixolon/Metapace T-3II (Luwosoft-Label)
PRINTER_VENDOR_ID = 0x1504
PRINTER_PRODUCT_ID = 0x002b
PRINTER_IN_EP = 0x81
PRINTER_OUT_EP = 0x02

# --- Wiimote / Balance-Board Report-IDs (siehe wiibrew.org) ---
CMD_LED = 0x11
CMD_REPORTING_MODE = 0x12
CMD_WRITE_MEMORY = 0x16
CMD_READ_MEMORY = 0x17

IN_STATUS = 0x20
IN_READ_DATA = 0x21
IN_EXT_8BYTES = 0x32

L2CAP_PSM_CONTROL = 0x11
L2CAP_PSM_INTERRUPT = 0x13

TOP_RIGHT, BOTTOM_RIGHT, TOP_LEFT, BOTTOM_LEFT = 0, 1, 2, 3

# Nach einer Messung nur ganz kurz warten, dann sofort wieder auf
# Verbindung lauern - das Board schaltet sich nach dem Trennen selbst ab,
# der naechste Power-Knopf-Druck wird so ohne Wartezeit erkannt.
RECONNECT_DELAY = 1  # Sekunde

HA_DISCOVERY_PREFIX = "homeassistant"
HA_STATE_TOPIC = "wiiboard/weight"
HA_OBJECT_ID = "wii_balance_board"


class BoardEvent:
    def __init__(self, top_left, top_right, bottom_left, bottom_right,
                 button_pressed, button_released):
        self.top_left = top_left
        self.top_right = top_right
        self.bottom_left = bottom_left
        self.bottom_right = bottom_right
        self.button_pressed = button_pressed
        self.button_released = button_released
        self.total_weight = top_left + top_right + bottom_left + bottom_right


class WiiBoard:
    def __init__(self, address):
        self.address = address
        self.control_sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        self.data_sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        # calibration[0] = 0kg, [1] = 17kg, [2] = 34kg, je 4 Sensoren
        self.calibration = [[10000] * 4 for _ in range(3)]
        self.last_event = BoardEvent(0, 0, 0, 0, False, False)

    def connect(self):
        # Reihenfolge (Interrupt vor Control) so uebernommen wie im
        # gr8w8upd8m8-Referenzscript - das Board reagiert darauf zuverlaessig.
        self.data_sock.connect((self.address, L2CAP_PSM_INTERRUPT))
        self.control_sock.connect((self.address, L2CAP_PSM_CONTROL))
        self._read_calibration()
        # Extension aktivieren (Verschluesselung der Erweiterung deaktivieren)
        self._send(CMD_WRITE_MEMORY, bytes([0x04, 0xA4, 0x00, 0x40, 0x00]))
        # Kontinuierliches Reporting im Balance-Board-Modus (Report 0x32)
        self._send(CMD_REPORTING_MODE, bytes([0x04, IN_EXT_8BYTES]))

    def _send(self, report_id, data: bytes):
        self.control_sock.send(bytes([0x52, report_id]) + data)

    def _read_calibration(self):
        # Read Memory: flags 0x04 (Extension-Adressraum), Adresse 0xA40024,
        # Laenge 0x18 (24 Byte)
        self._send(CMD_READ_MEMORY, bytes([0x04, 0xA4, 0x00, 0x24, 0x00, 0x18]))
        received = b""
        while len(received) < 24:
            packet = self.data_sock.recv(64)
            if packet[1] == IN_READ_DATA:
                size = (packet[4] >> 4) + 1
                received += packet[7:7 + size]
        for i in range(2):
            for j in range(4):
                idx = (i * 4 + j) * 2
                self.calibration[i][j] = (received[idx] << 8) | received[idx + 1]
        for j in range(4):
            idx = 16 + j * 2
            self.calibration[2][j] = (received[idx] << 8) | received[idx + 1]

    def set_light(self, on: bool):
        self._send(CMD_LED, bytes([0x10 if on else 0x00]))

    def _calc_mass(self, raw, sensor):
        c0, c1, c2 = (self.calibration[i][sensor] for i in range(3))
        if raw < c0:
            return 0.0
        if raw < c1:
            return 17.0 * (raw - c0) / float(c1 - c0)
        return 17.0 + 17.0 * (raw - c1) / float(c2 - c1)

    def _parse_event(self, payload: bytes):
        button_state = (payload[0] << 8) | payload[1]
        button_pressed = button_state == 0x08
        button_released = (not button_pressed) and self.last_event.button_pressed

        raw_tr = (payload[2] << 8) | payload[3]
        raw_br = (payload[4] << 8) | payload[5]
        raw_tl = (payload[6] << 8) | payload[7]
        raw_bl = (payload[8] << 8) | payload[9]

        event = BoardEvent(
            self._calc_mass(raw_tl, TOP_LEFT),
            self._calc_mass(raw_tr, TOP_RIGHT),
            self._calc_mass(raw_bl, BOTTOM_LEFT),
            self._calc_mass(raw_br, BOTTOM_RIGHT),
            button_pressed, button_released,
        )
        self.last_event = event
        return event

    def read_events(self):
        """Generator: liefert BoardEvent-Objekte, solange die Verbindung steht."""
        while True:
            packet = self.data_sock.recv(64)
            if not packet:
                break
            report_id = packet[1]
            if report_id == IN_EXT_8BYTES:
                yield self._parse_event(packet[2:12])
            # IN_STATUS (Batterie etc.) wird hier ignoriert

    def disconnect(self):
        for s in (self.data_sock, self.control_sock):
            try:
                s.close()
            except OSError:
                pass


def do_measurement(address, weight_threshold=20):
    """Verbindet einmal, misst, trennt wieder. Gibt das Gewicht zurueck (oder None)."""
    board = WiiBoard(address)
    board.connect()
    board.set_light(True)

    readings = []
    measuring = False
    try:
        for event in board.read_events():
            if event.total_weight > weight_threshold:
                readings.append(event.total_weight)
                measuring = True
                print(f"{event.total_weight:6.1f} kg", end="\r")
            elif measuring:
                break
    finally:
        # LED aus als sichtbares Feedback "Messung fertig", dann trennen
        try:
            board.set_light(False)
        except OSError:
            pass
        board.disconnect()

    if not readings:
        return None
    histogram = collections.Counter(round(w, 1) for w in readings)
    return histogram.most_common(1)[0][0]


def print_receipt(weight):
    """Druckt einen kleinen Bon mit dem Messergebnis auf dem Metapace T-3II."""
    if Usb is None:
        print("python-escpos ist nicht installiert - kein Druck moeglich. "
              "Installieren mit: pip3 install python-escpos --break-system-packages")
        return
    printer = None
    try:
        printer = Usb(PRINTER_VENDOR_ID, PRINTER_PRODUCT_ID, 0,
                      in_ep=PRINTER_IN_EP, out_ep=PRINTER_OUT_EP)
        printer.set(align="center", bold=True, width=2, height=2)
        printer.text(f"{weight:.1f} kg\n")
        printer.set(align="center", bold=False, width=1, height=1)
        printer.text(datetime.datetime.now().strftime("%d.%m.%Y  %H:%M:%S\n"))
        printer.text("\n")
        printer.cut()
    except Exception as exc:  # USB-Fehler nicht die Messschleife abbrechen lassen
        print(f"Druckfehler: {exc}")
    finally:
        # Interface wieder freigeben, sonst schlaegt der naechste Druck mit
        # "Resource busy" fehl, weil das Geraet noch "claimed" ist.
        if printer is not None:
            try:
                printer.close()
            except Exception:
                pass


class HomeAssistantPublisher:
    """Kapselt MQTT-Verbindung + Home-Assistant-Discovery fuers Balance Board."""

    def __init__(self, host, port, username, password):
        if mqtt is None:
            raise RuntimeError(
                "paho-mqtt ist nicht installiert. "
                "Installieren mit: pip3 install paho-mqtt --break-system-packages"
            )
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="wiiboard",
        )
        if username:
            self.client.username_pw_set(username, password)
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()
        self._publish_discovery()

    def _publish_discovery(self):
        config_topic = (
            f"{HA_DISCOVERY_PREFIX}/sensor/{HA_OBJECT_ID}/config"
        )
        payload = (
            '{'
            f'"name": "Wii Balance Board Gewicht", '
            f'"unique_id": "{HA_OBJECT_ID}", '
            f'"state_topic": "{HA_STATE_TOPIC}", '
            f'"unit_of_measurement": "kg", '
            f'"device_class": "weight", '
            f'"icon": "mdi:scale-bathroom"'
            '}'
        )
        self.client.publish(config_topic, payload, retain=True)

    def publish_weight(self, weight):
        self.client.publish(HA_STATE_TOPIC, f"{weight:.1f}", retain=True)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Wii Balance Board -> Home Assistant")
    parser.add_argument("address", help="Bluetooth-MAC-Adresse des Boards")
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST"),
                         help="MQTT-Broker-Adresse (oder MQTT_HOST env var)")
    parser.add_argument("--mqtt-port", type=int,
                         default=int(os.environ.get("MQTT_PORT", 1883)))
    parser.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"))
    parser.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--print", action="store_true", dest="print_receipt",
                         help="Nach jeder Messung einen Bon auf dem Metapace T-3II drucken")
    args = parser.parse_args()

    publisher = None
    if args.mqtt_host:
        publisher = HomeAssistantPublisher(
            args.mqtt_host, args.mqtt_port, args.mqtt_user, args.mqtt_password)
        print(f"MQTT verbunden mit {args.mqtt_host}:{args.mqtt_port}, "
              f"Topic: {HA_STATE_TOPIC}")
    else:
        print("Kein --mqtt-host angegeben - Messungen werden nur lokal ausgegeben.")

    print("Warte auf Board (einfach normalen Power-Knopf druecken, "
          "kein Sync-Knopf noetig)... Strg+C zum Beenden.")

    try:
        while True:
            try:
                weight = do_measurement(args.address)
            except OSError:
                # Board noch nicht erreichbar/eingeschaltet - kurz warten, erneut versuchen
                time.sleep(1)
                continue

            if weight is not None:
                print(f"\nGemessenes Gewicht: {weight} kg\n")
                if publisher:
                    publisher.publish_weight(weight)
                if args.print_receipt:
                    print_receipt(weight)
            print("Warte auf naechste Messung...")
            time.sleep(RECONNECT_DELAY)
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        if publisher:
            publisher.close()


if __name__ == "__main__":
    main()
