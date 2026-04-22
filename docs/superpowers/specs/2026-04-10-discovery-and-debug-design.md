# Discovery Arrays + Debug System Design

## Overview

Add network device discovery with 50-slot output arrays and a multi-layer debug system with configurable output routing to the Apple TV 4-Series control module.

## 1. Discovery

### Behavior
- **Auto-discover on startup**: After bridge reports `BRIDGE_READY`, SIMPL+ sends `DISCOVER` command
- **Manual re-scan**: Pulse `Discover` digital input to re-scan
- Each scan clears previous results and repopulates arrays

### Protocol
- SIMPL+ sends: `DISCOVER`
- Bridge performs `pyatv.scan(timeout=5)` with no host filter (finds all devices on network)
- Bridge responds: `SCAN_RESULTS:<json_array>`
  - Example: `SCAN_RESULTS:[{"name":"Living Room","model":"Apple TV 4K","address":"10.100.51.132"}]`
- SIMPL+ parses JSON array, populates output arrays

### SIMPL+ Signals

| Signal | Type | Size | Description |
|---|---|---|---|
| `Discover` | DIGITAL_INPUT | - | Pulse to re-scan network |
| `Discovered_Device_Count` | ANALOG_OUTPUT | - | Number of devices found (0-50) |
| `Discovered_Device_Name$` | STRING_OUTPUT[50] | 64 chars | Device friendly names |
| `Discovered_Device_Model$` | STRING_OUTPUT[50] | 64 chars | Device model strings |
| `Discovered_Device_IP$` | STRING_OUTPUT[50] | 32 chars | Device IP addresses |

## 2. Debug System

### Debug Levels (Analog Input)

| Value | Meaning | Sources Enabled |
|---|---|---|
| 0 | No debug output | None |
| 1 | SIMPL+ only | `[SPLUS]` prefixed messages |
| 2 | SimplSharp only | `[SS]` prefixed messages |
| 3 | Python only | `[PY]` prefixed messages |
| 4 | All layers | All prefixed messages |

### Output Routing (String Parameter)

`STRING_PARAMETER Print_Location` with allowed values:

| Value | Trace() | Print() | ErrorLog | DebugOutput$ |
|---|---|---|---|---|
| `Trace` | Y | - | - | - |
| `Print` | - | Y | - | - |
| `ErrLog` | - | - | Y | - |
| `DebugOutput$` | - | - | - | Y |
| `All - No ErrLog` | Y | Y | - | Y |
| `All` | Y | Y | Y | Y |

### SIMPL+ Signals

| Signal | Type | Description |
|---|---|---|
| `DebugLevel` | ANALOG_INPUT | 0-4, controls which layers produce debug output |
| `Print_Location` | STRING_PARAMETER | Compile-time setting for output routing |
| `DebugOutput$` | STRING_OUTPUT | All enabled debug messages with layer prefix |

### Debug Message Flow

All debug messages use `DEBUG:` prefix in the inter-process protocol:

```
Python:  mod.set("DEBUG:[PY] scanning for devices...")
         -> SIMPL+ HandleBridgeData -> check level -> DebugMsg routes per Print_Location

C#:      delegate callback "DEBUG:[SS] deploying scripts..."
         -> SIMPL+ HandleInstallerData -> check level -> DebugMsg routes per Print_Location

SIMPL+:  DebugMsg(1, "sending INIT command")
         -> adds [SPLUS] prefix -> routes per Print_Location
```

### Debug Helper Function (SIMPL+)

```
Function DebugMsg(INTEGER iLevel, STRING sMsg)
{
    STRING sOut[512];
    If (DebugLevel = 0) Return;
    If ((DebugLevel <> 4) && (DebugLevel <> iLevel)) Return;

    // Build prefixed message (prefix already in sMsg for [SS] and [PY])
    sOut = sMsg;

    // Route based on Print_Location parameter
    If (Print_Location = "Print" || Print_Location = "All - No ErrLog" || Print_Location = "All")
        Print("%s\n", sOut);
    If (Print_Location = "Trace" || Print_Location = "All - No ErrLog" || Print_Location = "All")
        Trace("%s", sOut);
    If (Print_Location = "ErrLog" || Print_Location = "All")
        // Use ErrorLog via C# since SIMPL+ has no direct ErrorLog
    If (Print_Location = "DebugOutput$" || Print_Location = "All - No ErrLog" || Print_Location = "All")
        DebugOutput$ = sOut;
}
```

### Debug Level Propagation

When `DebugLevel` changes:
1. SIMPL+ stores locally for `[SPLUS]` filtering
2. SIMPL+ sends `SET_DEBUG:N` to Python bridge
3. Python bridge stores level, only sends `DEBUG:` messages when enabled
4. C# receives level via property, only sends `DEBUG:` messages when enabled

## 3. Files Modified

| File | Changes |
|---|---|
| `AppleTV_4Series_v5.usp` | Add signals, DebugMsg function, discovery parsing, debug routing, Print_Location parameter |
| `AppleTVSetup.cs` (INSTALLER_PY) | No changes needed |
| `AppleTVSetup.cs` (BRIDGE_PY) | Add DISCOVER handler, SET_DEBUG handler, debug-gated output |
| `AppleTVSetup.cs` (C# class) | Add DebugLevel property, debug-gated ErrorLog output |
