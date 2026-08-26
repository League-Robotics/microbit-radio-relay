"""Constants shared by the hardware-free tests.

Deliberately NOT in conftest.py: there are two conftest modules in this tree
(tests/ and tests/hil/), they share a basename, and `from conftest import ...`
resolves to whichever one pytest inserted into sys.path first -- which depends
on the directory you ran pytest from. A unique module name has no such ambiguity.
"""

# Real DAPLink UIDs share a prefix AND a suffix; only the middle distinguishes
# them. These fixtures preserve that so tests catch code that slices the wrong end.
UID_A = "9906360200052820aaaa2372c44f4f67000000006e052820"
UID_B = "9906360200052820bbbb6c3809a44554000000006e052820"
PORT_A = "/dev/fake-a"
PORT_B = "/dev/fake-b"
