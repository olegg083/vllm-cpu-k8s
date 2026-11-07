#!/usr/bin/env python3

import importlib.metadata as md
import re
import sys

IGNORED = {"pip", "setuptools", "wheel"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main(path: str) -> int:
    installed = {norm(d.metadata["Name"]): d.version for d in md.distributions()}
    pinned: dict[str, str | None] = {}
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if " @ " in line:
            pinned[norm(line.split(" @ ", 1)[0])] = None
        elif "==" in line:
            name, version = line.split("==", 1)
            pinned[norm(name)] = version.strip()
        else:
            print(f"ERROR: строка без точной версии (==): {line!r}")
            return 1

    problems = []
    for name, version in pinned.items():
        if name in IGNORED:
            continue
        if name not in installed:
            problems.append(f"в requirements.txt есть, но не установлен: {name}")
        elif version is not None and installed[name] != version:
            problems.append(f"{name}: requirements={version}, установлено={installed[name]}")
    for name in sorted(set(installed) - set(pinned) - IGNORED):
        problems.append(f"установлен, но не зафиксирован в requirements.txt: {name}=={installed[name]}")

    if problems:
        print("requirements.txt расходится с окружением образа:")
        print("\n".join(f"  - {p}" for p in problems))
        return 1
    print(f"OK: {len(pinned)} пакетов совпадают с requirements.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"))
