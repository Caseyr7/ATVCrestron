# Crestron 4-Series Python Programming Reference

## System Requirements
- **4-Series firmware**: v2.7000.0031 or greater
- **Include4.dat**: v2.17.073 or greater
- **Python version on processor**: 3.8.2 (ARM 32-bit / armv7l)
- **SIMPL Windows**: v4.1700.03.01 or greater
- **SimplPlusPythonAdapter.clz**: Supplied via Crestron Database

## Python Entry Point
- Every Python script MUST define `def crestron_main(module_info_object):` as its entry point
- The `module_info_object` parameter provides:
  - `module_info_object.uid` — unique GUID identifier
  - `module_info_object.args` — arguments passed via RunWithArgs
  - `module_info_object.set(string)` — send data back to SIMPL+ (triggers DataReceived callback)
  - `module_info_object.subscribe(callback_func)` — register to receive data from SIMPL+ (when SendData is called)
- Communication is NOT via stdin/stdout — use `mod.set()` and `mod.subscribe()`

## SIMPL+ Python Adapter API
```
#CRESTRON_SIMPLSHARP_LIBRARY "SimplPlusPythonAdapter"

// Run a Python script
PythonInterface.Run(uniqueID, scriptPath, pythonModuleRef)  // returns SIGNED_LONG_INTEGER (0=success)
PythonInterface.RunWithArgs(uniqueID, scriptPath, pythonModuleRef, argsList)

// Receive data from Python (when Python calls mod.set())
RegisterDelegate(PythonInterface, DataReceived, CallbackFunction)
// Callback signature:
CALLBACK FUNCTION OnData(PythonModule sender, PythonAdapterDataReceivedEventArgs args)

// Send data to Python (triggers Python's subscribe callback)
pModule.SendData(jsonString)
PythonInterface.SendData(uniqueID, jsonString)

// Stored data
PythonInterface.SendDataStored(uniqueID, data)  // sends and stores in JsonDb
PythonInterface.GetData(uniqueID)                // retrieve stored data
PythonInterface.ClearData(uniqueID)              // clear stored data

// Utility
PythonAdapterUtils.NewGuid  // generate unique ID
pModule.UniqueIdentifier    // get module's GUID
pModule.State               // "Running" or "Stopped"
pModule.File                // script path
pModule.ArgumentCount       // number of args
pModule.Arguments[i]        // get argument by index
pModule.Restart()           // restart module
```

## Filesystem (CRITICAL)
- **Crestron SFTP/CrestronIO path**: `/user/program01/` → maps to real filesystem `/simpl/app01/`
- **CrestronIO** (`Crestron.SimplSharp.CrestronIO`): sees a virtual filesystem; `/User/` maps to physical `/user/` (lowercase)
- **System.IO**: sees the real Linux filesystem as-is (case-sensitive)
- **Program CWD**: `/simpl/app01/` — visible to BOTH CrestronIO and System.IO (the shared directory)
- **Python scripts**: deployed to program CWD so both SIMPL+ and Python can access them
- **CRITICAL**: ALL writable filesystems are mounted `noexec` — compiled Python extensions (.so files) CANNOT be loaded from any writable directory
  - `/user` ext4 rw,nosuid,nodev,**noexec** — writable but no exec
  - `/user/cert` ext4 rw,nosuid,nodev,**noexec** — writable but no exec
  - `/simpl/app00` ext4 rw,nosuid,nodev,**noexec** — writable but no exec
  - `/tmp/` — permission denied for writing
- **No pip**: `/usr/bin/pip3` does not exist, `python3 -m pip` returns "No module named pip"
- **Implication**: Only pure-Python packages or packages already installed in the read-only system image can be used

### memfd_create Bypass for noexec (CONFIRMED WORKING)
- `memfd_create` syscall IS supported (kernel allows it)
- `/proc` is mounted `rw,relatime` with **NO noexec flag** — `/proc/self/fd/N` paths ARE executable
- **Technique**: Read .so file into memory → `memfd_create()` → write bytes → `os.lseek(fd, 0, os.SEEK_SET)` → symlink original path to `/proc/self/fd/N`
- `dlopen` can then load the .so via the symlink to the memfd
- **CRITICAL: Must `os.lseek(fd, 0, os.SEEK_SET)` after writing** — dlopen reads from current position, which is EOF after write
- **Keep fd references alive** — store in a global list to prevent garbage collection closing the fd

### ABI Suffix Mismatch (CRITICAL)
- Docker `--platform linux/arm/v7` builds produce `.cpython-38-arm-linux-gnueabi**hf**.so` (hard-float)
- Crestron Python 3.8.13 expects `.cpython-38-arm-linux-gnueabi.so` (NO `hf` suffix)
- Python's `importlib.machinery.EXTENSION_SUFFIXES` = `['.cpython-38-arm-linux-gnueabi.so', '.abi3.so', '.so']`
- **Files MUST be renamed** from `gnueabihf.so` → `gnueabi.so` before Python can find them
- The actual binary code is compatible (both are armv7l) — only the filename tag differs
- This rename must happen BEFORE memfd patching (rename the actual files, then create memfd copies)

## Installing Python Dependencies
- **There is NO pip on the processor** (despite docs mentioning `pip3 install debugpy` — that command is for your workstation)
- **Workflow**: Install packages on your PC, then upload to the processor
- **Per-program**: Upload deps to the program slot directory (e.g., `/user/program01/deps/` via SFTP)
- **Global**: Upload deps to a shared location, update ModulePath in your script

### Building ARM 32-bit Packages (for CP4)
The CP4 processor is ARM 32-bit (armv7l). Use Docker to build compatible packages:

```bash
# Clean target
rmdir /s /q C:\temp\pyatv_deps
mkdir C:\temp\pyatv_deps

# Build in ARM32 Docker container (only binary wheels, fast)
docker run --rm --platform linux/arm/v7 -v C:\temp\pyatv_deps:/deps python:3.8-bullseye bash -c "pip install --upgrade pip && pip install <package> --only-binary=:all: --target /deps"

# If some packages lack armv7l wheels, build from source (slow, 20-30 min):
docker run --rm --platform linux/arm/v7 -v C:\temp\pyatv_deps:/deps python:3.8-bullseye bash -c "apt-get update && apt-get install -y gcc g++ libffi-dev libssl-dev && pip install --upgrade pip setuptools wheel && pip install <package> --target /deps"

# For packages without armv7l wheels, install without deps then add deps individually:
docker run --rm --platform linux/arm/v7 ... bash -c "pip install <package> --no-deps --target /deps && pip install --only-binary=:all: --target /deps <dep1> <dep2> ..."
```

### Key Issues Encountered
- `--only-binary=:all:` is REQUIRED when using `--platform` flag with pip
- `miniaudio` package has NO armv7l wheels — must be stubbed out if not needed
- `cryptography` .so files require exec permission — use memfd_create bypass (see above)
- **ABI suffix**: Docker ARM builds produce `gnueabihf` but Crestron Python expects `gnueabi` — rename required
- Newer Python packages may have cp38 wheel tags but use Python 3.9+ syntax (e.g., `ABCMeta object is not subscriptable`, `type object is not subscriptable`)
- `typing_extensions` version must be compatible with 3.8 (v4.7.1 is safe)

## SIMPL+ Syntax Rules
- Use `{ }` braces (NOT `BEGIN...END`)
- String functions: `ItoA()` (not `IToS()`), `Chr(10)` (not `\n`)
- No string concatenation inside function call arguments — pre-build in local variables
- `STRING_OUTPUT` declarations have NO buffer size and can only be written to (not read)
- `//` style comments for module header
- `#LARGE_STACK_SIZE` is NOT supported on 4-series
- Callback delegate from C#: STRING parameters are wrapped as `SimplSharpString`

## C# SIMPL# Library Notes
- Target: .NET Framework 4.7.2, x86
- NuGet: `Crestron.SimplSharp.SDK.Library` v2.21.237
- Use `SimplSharpString` (not `string`) for delegate parameters passed to SIMPL+
- `CrestronIO.File` and `System.IO.File` see different filesystems
- Deploy scripts via `System.IO.File.WriteAllText()` to `System.IO.Directory.GetCurrentDirectory()`

## Debugging Python on 4-Series
- Install `debugpy` on your workstation: `pip3 install debugpy`
- Upload debugpy package to processor (same as any other dependency)
- In your Python script:
  ```python
  import debugpy
  def crestron_main(mod):
      debugpy.listen(('0.0.0.0', 5678))
      debugpy.wait_for_client()  # blocks until VS Code connects
      # ... rest of code
  ```
- Connect from VS Code using "Remote Attach" debug configuration
- Note: `wait_for_client()` blocks ALL communication until debugger connects
- Must restart program between debug sessions
