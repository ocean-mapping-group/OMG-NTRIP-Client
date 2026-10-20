OMG NTRIP RTCM Logger & Bridge v11
=================================

BUILD (Windows)
1. Extract all files together.
2. Run build_windows.bat on a Windows computer with Python installed.
3. Copy dist\OMG_NTRIP_RTCM_Logger_Bridge.exe to the destination PC.
   Python is NOT required on the destination PC.

CHANGES IN v11
- Tabs: Dashboard, Caster, Position, Outputs, Settings.
- Persistent Start/Stop/status controls and live event console below the tabs.
- Live RTCM3 frame validation with CRC-24Q, valid/bad counts and message types.
- GGA position, fix quality, satellite count and age monitoring.
- Separate output byte counts/errors, TCP client count, serial connected status.
- RTCM file rotation: every caster connection (default), UTC hourly,
  or by file size in MB. Every segment has a unique UTC timestamp and counter.
- Named configuration profiles, stored in APPDATA\NTRIP_RTCM_Bridge\profiles.
  Passwords are NEVER saved. Profile names allow letters/numbers/spaces/_/-.
- Original features retained: caster mountpoint search, serial/static GGA,
  automatic reconnection, file/serial/TCP/UDP outputs, OMG icon.

NOTES
- Output counters indicate attempted/successful API writes, NOT guaranteed
  receipt by a remote device. TCP server with zero clients does not deliver data.
- CRC checks validate RTCM3 framing and payload integrity, NOT GNSS accuracy.
- File writes are unbuffered, and active files are synced periodically.
- On hourly/size rotation, the active file may span caster reconnections.
  All reconnects are separately logged in connection_events.csv.
- Do not use the same serial COM port in Lefebure and this application.
- Static GGA must use the correct coordinates. GGA altitude is encoded with
  zero geoid separation, as in prior versions.
- Only NTRIP v1 raw streams supported; chunked transfer is unsupported.
- Do not enable forwarding to input COM with static GGA.
- Changing configuration requires stopping the worker first.
- For field operations, test output streams, reconnection, and Windows Firewall
  permissions on the target machine before relying on the data.

Version 12: Settings > Appearance includes OMG Light (default) and OMG Dark. The choice is saved in settings.json and profiles via the standard settings export.
