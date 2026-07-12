# Wii Balance Board → Home Assistant (+ Bon-Drucker)

Liest ein Wii Fit Balance Board über eine rohe Bluetooth-L2CAP-Verbindung
aus (Python-Standardbibliothek, kein `cwiid`, kein `xwiimote`, kein
`PyBluez`) und veröffentlicht jede Messung per MQTT in Home Assistant.
Optional wird bei jeder Messung automatisch ein Bon auf einem
ESC/POS-USB-Thermodrucker ausgedruckt.

Läuft auf einem Raspberry Pi (getestet auf einem Pi Zero W) mit
Bluetooth Classic (BR/EDR) an Bord.

## Warum kein cwiid/xwiimote?

Beide Projekte sind seit Jahren unmaintained und auf aktuellem
Raspberry Pi OS mit modernem BlueZ-5-Stack kaum noch zuverlässig zum
Laufen zu bringen. Dieses Script spricht das Wiimote-/Balance-Board-
Protokoll direkt über `socket.AF_BLUETOOTH` / `socket.BTPROTO_L2CAP`
(seit Python 3.3 in der Standardbibliothek enthalten) – keine externen
Bluetooth-Libraries nötig.

Protokoll-Referenz: [skorokithakis/gr8w8upd8m8](https://github.com/skorokithakis/gr8w8upd8m8)
(LGPL) sowie die bei [wiibrew.org](https://wiibrew.org) dokumentierten
Wiimote-/Balance-Board-HID-Reports.

## Features

- Auslesen von Gewicht und den 4 Einzelsensoren (vorne links/rechts,
  hinten links/rechts)
- Automatische Kalibrierung direkt aus dem im Board gespeicherten
  Kalibrierungsspeicher (0 kg / 17 kg / 34 kg Referenzpunkte)
- Schneller Reconnect-Loop: einmal synchronisieren (roter Knopf), danach
  reicht der normale Power-Knopf am Board
- MQTT-Publish inkl. Home-Assistant-MQTT-Discovery – der Sensor
  „Wii Balance Board Gewicht“ taucht automatisch in HA auf
- Optionaler Bon-Druck auf einem ESC/POS-USB-Thermodrucker
  (getestet mit Bixolon/Metapace T-3II, Vendor `1504`/Product `002b`)
- Läuft als systemd-Service im Hintergrund

## Hardware-Voraussetzungen

- Wii Fit Balance Board (4x AA-Batterien)
- Raspberry Pi mit eingebautem Bluetooth Classic (Pi 3B/3B+/4/Zero W/
  Zero 2 W – **kein** reiner ESP32-S2/S3/C3, die haben kein BT Classic)
- Optional: ESC/POS-fähiger USB-Bon-Drucker

## Installation

```bash
sudo apt-get update
sudo apt-get install libusb-1.0-0-dev   # nur falls Bon-Druck genutzt wird
sudo pip3 install paho-mqtt --break-system-packages
sudo pip3 install python-escpos pyusb --break-system-packages   # nur für Bon-Druck
```

> `sudo pip3 install ...` ist wichtig: Wird das Script per systemd/`sudo`
> als root ausgeführt, sieht es nur root-eigene, nicht user-eigene
> Python-Pakete.

## MAC-Adresse des Boards ermitteln

```bash
bluetoothctl
scan on
```
Roten Sync-Knopf im Batteriefach drücken – das Board erscheint als
`Nintendo RVL-WBC-01`. Adresse notieren, dann `scan off`.

## Erste Verbindung (einmalig)

Beim allerersten Verbinden **muss** der rote Sync-Knopf im Batteriefach
gedrückt werden – dabei merkt sich das Board dauerhaft die
Bluetooth-Adresse des Pis. Danach reicht der normale Power-Knopf vorne
am Board.

```bash
sudo python3 wiiboard.py AA:BB:CC:DD:EE:FF
```
(Roten Sync-Knopf drücken, sobald „Warte auf Board …“ erscheint.)

## Verwendung

```bash
sudo python3 wiiboard.py AA:BB:CC:DD:EE:FF \
  --mqtt-host 192.168.x.x \
  --mqtt-user MQTT_USER \
  --mqtt-password MQTT_PASSWORT \
  --print
```

| Argument           | Beschreibung                                          |
|---------------------|--------------------------------------------------------|
| `address`           | Bluetooth-MAC-Adresse des Boards (Pflichtargument)     |
| `--mqtt-host`        | MQTT-Broker-Adresse (auch via `MQTT_HOST` env möglich) |
| `--mqtt-port`        | Standard: 1883                                         |
| `--mqtt-user`        | auch via `MQTT_USER` env möglich                       |
| `--mqtt-password`    | auch via `MQTT_PASSWORD` env möglich                   |
| `--print`            | Nach jeder Messung einen Bon drucken                   |

Ohne `--mqtt-host` läuft das Script rein lokal mit Konsolenausgabe.

## Bon-Drucker (optional)

Getestet mit dem Bixolon/Metapace T-3II (Vendor `1504`, Product `002b`).
Für ein anderes Modell müssen ggf. `PRINTER_VENDOR_ID`,
`PRINTER_PRODUCT_ID`, `PRINTER_IN_EP` und `PRINTER_OUT_EP` im Script
angepasst werden. Die Endpoints lassen sich so auslesen:

```bash
sudo python3 -c "
import usb.core, usb.util
dev = usb.core.find(idVendor=0x1504, idProduct=0x002b)
for cfg in dev:
    for intf in cfg:
        for ep in intf:
            d = 'IN' if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else 'OUT'
            print(f'Interface {intf.bInterfaceNumber}: {hex(ep.bEndpointAddress)} ({d})')
"
```

### Wichtig: Kernel-Treiber-Konflikt (`usblp`)

Linux erkennt viele USB-Bon-Drucker automatisch als generisches
Druckerklassen-Gerät und lädt den `usblp`-Kernel-Treiber. Der blockiert
dann den direkten ESC/POS-Rohzugriff über `pyusb` (`Resource busy`).
Fix per udev-Regel, die `usblp` für genau dieses Gerät deaktiviert:

```bash
sudo tee /etc/udev/rules.d/99-metapace-printer.rules <<'EOF'
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="1504", ATTR{idProduct}=="002b", RUN+="/bin/sh -c 'echo -n $kernel > /sys/bus/usb/drivers/usblp/unbind 2>/dev/null || true'"
EOF
sudo udevadm control --reload-rules
```

## Als Hintergrunddienst (systemd)

```bash
sudo mkdir -p /opt/wiiboard
sudo cp wiiboard.py /opt/wiiboard/
```

MQTT-Zugangsdaten in eine Umgebungsdatei auslagern:
```bash
sudo tee /etc/wiiboard.env <<'EOF'
MQTT_HOST=192.168.x.x
MQTT_USER=dein_user
MQTT_PASSWORD=dein_passwort
EOF
sudo chmod 600 /etc/wiiboard.env
```

Service-Unit anlegen (`/etc/systemd/system/wiiboard.service`):
```ini
[Unit]
Description=Wii Balance Board -> Home Assistant Bridge
After=bluetooth.target network-online.target
Wants=bluetooth.target network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/wiiboard.env
ExecStart=/usr/bin/python3 /opt/wiiboard/wiiboard.py AA:BB:CC:DD:EE:FF --print
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

Aktivieren:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wiiboard.service
sudo journalctl -u wiiboard.service -f
```

`User=root` ist notwendig, da rohe Bluetooth-L2CAP-Sockets
Root-Rechte benötigen.

## Funktionsweise / Protokoll-Kurzüberblick

- Zwei L2CAP-Kanäle: Control (PSM `0x11`) und Interrupt/Data (PSM `0x13`)
- Ausgehende Reports (Host → Board) beginnen mit `0x52` (HID Set Report)
  über den Control-Kanal
- Eingehende Reports (Board → Host) kommen über den Interrupt-Kanal,
  Byte 1 enthält die Report-ID (`0x20` Status, `0x21` Read-Memory-Data,
  `0x32` Core Buttons + 8 Extension-Bytes = Balance-Board-Gewichtsdaten)
- Kalibrierungsdaten werden per Read-Memory-Report aus Adresse
  `0xA40024` gelesen (24 Byte: 0 kg / 17 kg Referenz je 4 Sensoren,
  danach 34 kg Referenz)
- Nach dem einmaligen Sync (roter Knopf) merkt sich das **Board selbst**
  die Host-Adresse dauerhaft; der Host muss dafür nichts weiter tun

## Bekannte Einschränkungen

- Reiner Client-Ansatz: Das Script verbindet sich aktiv zum Board. Der
  „nur Power-Knopf drücken, Board ruft den Host an“-Workflow (wie ihn
  die [ESPHome-Balance-Board-Component](https://github.com/gulrotkake/esphome/tree/balance-board)
  auf ESP32 umsetzt) würde eine Server-Rolle (lauschende L2CAP-Sockets)
  erfordern – aktuell nicht implementiert. In der Praxis reicht der
  schnelle Reconnect-Loop aber für alltägliches Wiegen völlig aus.
- Rauschschwelle für „Messung läuft“ ist auf 20 kg fest codiert
  (`weight_threshold` in `do_measurement()`), für andere Anwendungsfälle
  ggf. anpassen.
- Kein automatisches Zurückschalten auf Standby-Sparmodus – bei
  Dauerbetrieb (nicht empfohlen) sinkt die Batterielaufzeit deutlich
  unter die von Nintendo angegebenen ~60 Stunden Aktivbetrieb.

## Lizenz

Das Protokoll-Grundgerüst orientiert sich an
[skorokithakis/gr8w8upd8m8](https://github.com/skorokithakis/gr8w8upd8m8)
(LGPL).
