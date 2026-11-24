"""Build a Linux x86-64 bridge and package it for an offline NAS."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NAME = "FnOS-0.8.45-V100-20formats.zip"
SOURCES = (
    "LICENSE", "README.md", "bridge.c", "bridge.map", "bridge_test.py", "pixels.py",
    "docs/diagnosis.md", "docs/testing.md", "tests/v100_frames.py",
    "tests/v100_host_scale.py", "tools/build_release.py",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def compile_bridge(zig, directory):
    (directory / "unstripped").mkdir()
    full = directory / "unstripped" / "libfnbridge.so"
    binary = directory / "libfnbridge.so"
    subprocess.run([
        zig, "cc", "-target", "x86_64-linux-gnu.2.17", "-O2", "-shared", "-fPIC",
        "-Wall", "-Wextra", "-Werror", "-DTRANSFER_RVA=0x41850", "-DBUILD_ID_RVA=0x248",
        "bridge.c", "-Wl,--version-script=bridge.map", "-Wl,-z,now", "-Wl,-z,relro",
        "-ldl", "-lpthread", "-o", str(full),
    ], cwd=ROOT, check=True)
    subprocess.run([zig, "objcopy", "--strip-debug", str(full), str(binary)], check=True)
    return binary.read_bytes()


def package(binary, output):
    files = {name: (ROOT / name).read_text(encoding="utf-8").encode() for name in SOURCES}
    key = "bridge_test.py"
    files[key], count = re.subn(rb'(?m)^SO_HASH="[0-9a-f]{64}"$',
                                f'SO_HASH="{sha(binary)}"'.encode(), files[key])
    if count != 1:
        raise ValueError("Expected one controller digest")
    files["libfnbridge.so"] = binary
    files["SHA256SUMS.txt"] = "".join(f"{sha(data)}  {name}\n" for name, data in
                                     sorted(files.items())).encode()
    output.mkdir(parents=True, exist_ok=True)
    dest = output / NAME
    with zipfile.ZipFile(dest, "w") as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo("v100-formats/" + name, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
    (output / "SHA256SUMS.txt").write_bytes(f"{sha(dest.read_bytes())}  {NAME}\n".encode())
    return dest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zig", default="zig", help="Zig executable (tested with 0.14.1)")
    p.add_argument("--output", type=Path, default=ROOT / "dist")
    args = p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        print(package(compile_bridge(args.zig, Path(tmp)), args.output))


if __name__ == "__main__":
    main()
