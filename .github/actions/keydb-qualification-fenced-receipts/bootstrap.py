"""Fixed -I -S entry: hold exact pure SDK closure before any package/site execution."""

import hashlib
import importlib.abc
import importlib.util
import json
import os
import pathlib
import stat
import sys
import types

if sys.version_info[:3] != (3, 13, 9):
    raise SystemExit("Fenced producer interpreter HOLD")
site = pathlib.Path(sys.executable).parent.parent / "lib/python3.13/site-packages"
root = site / "just_akash"
pins = json.loads(sys.argv[1])
held = {}
for name, expected in pins.items():
    if not name.startswith("just_akash.") or name.count(".") != 1:
        raise SystemExit("Fenced producer source HOLD")
    file = root / (name.split(".")[1] + ".py")
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= 131072
        ):
            raise SystemExit("Fenced producer source HOLD")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(131073)
        after, named = os.fstat(fd), os.lstat(file)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        ):
            if getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(
                named, field
            ):
                raise SystemExit("Fenced producer source HOLD")
        if hashlib.sha256(raw).hexdigest() != expected or len(raw) != before.st_size:
            raise SystemExit("Fenced producer source HOLD")
        held[name] = compile(raw, str(file), "exec")
    finally:
        os.close(fd)


class Captured(importlib.abc.MetaPathFinder, importlib.abc.SourceLoader):
    unlocked = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname in held:
            return importlib.util.spec_from_loader(fullname, self)
        if fullname.startswith("just_akash.") and not self.unlocked:
            raise ImportError("Effect/helper runtime is closed before MAY_CREATE")
        return None

    def get_filename(self, fullname):
        if fullname not in held:
            raise ImportError("Source outside captured pure closure")
        return str(root / (fullname.split(".")[1] + ".py"))

    def get_data(self, path):
        raise OSError("No mutable source/cache reads permitted")

    def get_code(self, fullname):
        return held[fullname]


finder = Captured()
sys.meta_path.insert(0, finder)
package = types.ModuleType("just_akash")
package.__path__ = [str(root)]
sys.modules["just_akash"] = package
controller = importlib.import_module("just_akash.keydb_qualification_fenced_controller")


def unlock():
    # Called only AFTER the fixed controller irreversibly poisons its latch.
    finder.unlocked = True
    sys.path.append(str(site))


vars(controller)["enable_runtime"] = unlock
sys.argv = ["keydb-qualification-v1-fenced-controller"]
controller.main()
