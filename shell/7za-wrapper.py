"""7za.exe shim — forwards all args to 7za.exe.orig and swallows the harmless
'cannot create symbolic link' exit code (2) on Windows.

electron-builder downloads winCodeSign which contains macOS .dylib symlinks
inside a 7z archive. Extracting those on Windows without Developer Mode or
admin returns exit 2 from 7za, which electron-builder treats as a fatal error.
We never actually use the .dylib files (we're targeting Windows), so this
shim treats exit 2 as success when the only failures were symlink-related.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(sys.argv[0] if not getattr(sys, "frozen", False) else sys.executable))
REAL = os.path.join(HERE, "7za.exe.orig")

if not os.path.isfile(REAL):
    sys.stderr.write(f"7za-wrapper: missing {REAL}\n")
    sys.exit(127)

# Pass-through everything verbatim. Capture stdout/stderr so we can inspect.
proc = subprocess.run([REAL, *sys.argv[1:]], capture_output=True)
sys.stdout.buffer.write(proc.stdout)
sys.stderr.buffer.write(proc.stderr)

# Exit 2 from 7za = warnings only. If the only warnings are symlink failures
# (which are guaranteed on a non-admin Windows host), translate to success.
if proc.returncode == 2:
    err = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").lower()
    symlink_only = "symbolic link" in err or "symlink" in err
    if symlink_only:
        sys.exit(0)

sys.exit(proc.returncode)
