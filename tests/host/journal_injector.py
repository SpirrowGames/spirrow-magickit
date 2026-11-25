"""Write forged journal entries from inside a transient unit (host test helper).

Run as the main process of a throw-away system unit by
``test_journal_injection.py``. Sends native-protocol datagrams to journald
with values a hostile application could choose, then stays alive for a
moment so journald can still resolve this process's unit when it reads the
datagrams (a sender that has already exited loses its ``_SYSTEMD_UNIT``).

Usage: journal_injector.py <secret> <valid-message-id>
"""

import socket
import sys
import time

SECRET = sys.argv[1]
VALID_ID = sys.argv[2]

ENTRIES = [
    # 1. a well-formed entry: everything in it should come back
    {
        "MESSAGE": f"body with token={SECRET}",
        "PRIORITY": "3",
        "MESSAGE_ID": VALID_ID,
        "EXIT_STATUS": "3",
    },
    # 2. MESSAGE_ID carrying text
    {"MESSAGE": "x", "PRIORITY": "6", "MESSAGE_ID": f"token={SECRET}"},
    # 3. MESSAGE_ID that is hex but upper case / wrong length
    {"MESSAGE": "x", "PRIORITY": "6", "MESSAGE_ID": VALID_ID.upper()},
    # 4. PRIORITY out of range and as text
    {"MESSAGE": "x", "PRIORITY": "9"},
    {"MESSAGE": "x", "PRIORITY": SECRET},
    # 5. EXIT_STATUS as a signal name, out of range, and as text
    {"MESSAGE": "x", "PRIORITY": "6", "EXIT_STATUS": "TERM"},
    {"MESSAGE": "x", "PRIORITY": "6", "EXIT_STATUS": "256"},
    {"MESSAGE": "x", "PRIORITY": "6", "EXIT_STATUS": SECRET},
    # 6. trying to forge trusted fields and pose as another unit
    {"MESSAGE": "x", "PRIORITY": "6", "_PID": "1", "_SYSTEMD_UNIT": "spirrow-lexora.service"},
    {"MESSAGE": "x", "PRIORITY": "6", "UNIT": "spirrow-lexora.service"},
    # 7. free text in fields the probe never asks for
    {"MESSAGE": "x", "PRIORITY": "6", "SYSLOG_IDENTIFIER": SECRET, "CODE_FILE": SECRET},
]


def datagram(fields: dict[str, str]) -> bytes:
    return "".join(f"{k}={v}\n" for k, v in fields.items()).encode()


sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
for entry in ENTRIES:
    sock.sendto(datagram(entry), "/run/systemd/journal/socket")
time.sleep(3)
# (no output: a stdout line would be one more journal entry of this unit)
