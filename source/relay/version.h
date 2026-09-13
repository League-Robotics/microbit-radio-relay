// The relay firmware's build version, reported by !VER? (protocol §3.3) and
// shown in `mbrelay devices`.
//
// Kept equal to __version__ in server/src/mbrelay/__init__.py: a release tag
// builds both, so one string names both. Bump them together --
// server/tests/test_firmware_version.py fails if they drift.
#pragma once

#define RELAY_FIRMWARE_VERSION "0.20260913.2"
