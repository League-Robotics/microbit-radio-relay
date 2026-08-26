"""The systemd unit and udev rule, as strings.

Shipped inside the package so ``mbrelay install-unit --print`` works on any host,
including one where only the wheel landed. It prints; it never writes -- on the
fleet, installation is Ansible's job, and a command that quietly edits
/etc/systemd would fight the config management.
"""

SYSTEMD_UNIT = """\
[Unit]
Description=micro:bit radio relay TCP server
Documentation=https://github.com/Busboombot/microbit-radio-relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mbrelay
SupplementaryGroups=dialout plugdev
RuntimeDirectory=mbrelay
RuntimeDirectoryMode=0755
StateDirectory=mbrelay
ExecStart=/usr/local/bin/mbrelay serve --config /etc/mbrelay/mbrelay.toml
Restart=on-failure
RestartSec=3
KillSignal=SIGTERM
# Must exceed state.shutdown_grace_s (20s), or systemd kills the daemon while it
# is still handing boards back and they are left on the last client's channel.
TimeoutStopSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=no
DeviceAllow=char-ttyACM rw

[Install]
WantedBy=multi-user.target
"""

# SYMLINK+= gives a uid-stable path, so ttyACM renumbering across replugs stops
# mattering on Linux. macOS has no equivalent, which is why the inventory is
# keyed on the uid rather than on the device path.
UDEV_RULE = """\
# BBC micro:bit DAPLink interface (V1 and V2).
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="0d28", ATTRS{idProduct}=="0204", \\
  GROUP="dialout", MODE="0660", SYMLINK+="microbit/$attr{serial}"
"""
