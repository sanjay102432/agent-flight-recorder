"""Tool registry and the safe-for-prefetch taxonomy.

The taxonomy is the safety backbone of the whole system. Every tool is
registered with exactly one class, and the prefetch engine refuses to
speculatively run anything outside the safe set -- it refuses *silently and
structurally*, not with a warning, because prefetching an unsafe tool
"just to see what happens" is the exact failure this project exists to
make impossible.

    class        meaning                         prefetch?
    ---------------------------------------------------------
    read-only    reads state, returns text       YES
    pure         deterministic, no I/O at all    YES
    write        mutates the world               NO - never
    network      talks to the outside world       NO - never
    dangerous    obviously destructive            NO - never (alert on attempt)

The filesystem tools register per-run against a sandbox root: every rel_
path passed by the agent is normalized and rooted under that workspace, so
even the recording harness cannot escape it by accident.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

READ_ONLY = "read-only"
WRITE = "write"
NETWORK = "network"
DANGEROUS = "dangerous"
PURE = "pure"

# classes that may be speculatively executed
SPECULATIVE_CLASSES = frozenset({READ_ONLY, PURE})


ToolFn = Callable[..., Any]


class ToolSpec:
    """One registered tool: the function, its class, and short text used in
    the HTML report. ToolSpecs are immutable by convention."""

    __slots__ = ("name", "fn", "safety", "blurb", "arg_hint")

    def __init__(self, name: str, fn: ToolFn, safety: str, blurb: str, arg_hint: str) -> None:
        self.name = name
        self.fn = fn
        self.safety = safety
        self.blurb = blurb
        self.arg_hint = arg_hint
        if safety not in SPECULATIVE_CLASSES | {WRITE, NETWORK, DANGEROUS}:
            raise ValueError(f"safety class {safety!r} is not in the taxonomy")

    def describe(self) -> str:
        return f"{self.name} [{self.safety}] {self.blurb}"


class ToolRegistry:
    """Named tool set handed to an agent. Re-registering the same name with a
    *different* safety class is a bug at registration time, not a race the
    recorder has to catch later."""

    def __init__(self, root: Optional[str] = None) -> None:
        self.tools: Dict[str, ToolSpec] = {}
        self.root = os.path.abspath(root) if root else None

    def register(self, name: str, fn: ToolFn, safety: str, blurb: str, arg_hint: str) -> None:
        if name in self.tools and self.tools[name].safety != safety:
            raise ValueError(f"tool {name!r} re-registered with a different safety class")
        self.tools[name] = ToolSpec(name, fn, safety, blurb, arg_hint)

    def get(self, name: str) -> ToolSpec:
        if name not in self.tools:
            raise KeyError(f"tool {name!r} is not registered")
        return self.tools[name]

    def names(self) -> List[str]:
        return sorted(self.tools)

    def prefetchable(self) -> List[str]:
        return sorted(n for n, s in self.tools.items() if s.safety in SPECULATIVE_CLASSES)


def resolve_under_root(root: str, rel: str) -> str:
    """Root and normalize a relative path; refuse anything escaping the root."""
    if not rel:
        raise ValueError("empty path")
    if os.path.isabs(rel):
        raise ValueError(f"path must be relative: {rel!r}")
    norm = os.path.normpath(os.path.join(root, rel))
    root_norm = os.path.normpath(root)
    if os.path.commonpath([root_norm, norm]) != root_norm:
        raise ValueError(f"path escapes workspace: {rel!r}")
    return norm


def fs_list(workspace: str):
    """Returns (fn) where fn returns (result_text, receipt_dict)."""
    def fn(rel_dir: str = ".") -> Tuple[str, Dict[str, Any]]:
        target = resolve_under_root(workspace, rel_dir)
        if not os.path.isdir(target):
            raise FileNotFoundError(f"no such folder: {rel_dir}")
        names = sorted(os.listdir(target))
        receipt = {
            "kind": "fs_read",
            "read": [os.path.join(rel_dir, n) for n in names],
            "n_read": len(names),
        }
        return (("\n".join(names)) if names else "(empty)"), receipt
    return fn


def fs_read(workspace: str):
    def fn(rel_path: str) -> Tuple[str, Dict[str, Any]]:
        target = resolve_under_root(workspace, rel_path)
        if not os.path.isfile(target):
            raise FileNotFoundError(f"no such file: {rel_path}")
        with open(target, "r", encoding="utf-8") as f:
            data = f.read()
        receipt = {"kind": "fs_read", "read": [rel_path], "n_bytes": len(data)}
        return data, receipt
    return fn


def fs_write(workspace: str):
    def fn(rel_path: str, text: str) -> Tuple[str, Dict[str, Any]]:
        target = resolve_under_root(workspace, rel_path)
        existed = os.path.exists(target)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        receipt = {
            "kind": "fs_write",
            "written": [rel_path],
            "existed_before": existed,
            "n_bytes": len(text.encode("utf-8")),
        }
        return f"wrote {len(text)} chars to {rel_path}", receipt
    return fn


def shell_run(workspace: str):
    """Runs ONE shell command with cwd pinned to the sandbox.

    Receipt honesty: we record command + cwd + returncode. We do NOT claim
    to know every file the command touched -- the environment diff does
    that at run level. See README "Limits".
    """
    import subprocess

    def fn(command: str) -> Tuple[str, Dict[str, Any]]:
        proc = subprocess.run(
            command, shell=True, cwd=workspace,
            capture_output=True, text=True, timeout=60,
        )
        receipt = {
            "kind": "shell_execution",
            "cwd": workspace,
            "command": command,
            "returncode": proc.returncode,
        }
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        text = out if out else (err or f"(no output, exit {proc.returncode})")
        return text, receipt
    return fn


def make_tools(workspace: str) -> "ToolRegistry":
    """The standard v1 toolset, each entry sandboxed to ``workspace``."""
    reg = ToolRegistry(workspace)
    reg.register("fs.list", fs_list(workspace), READ_ONLY,
                 "List files in a folder relative to the workspace.", 'rel_dir="."')
    reg.register("fs.read", fs_read(workspace), READ_ONLY,
                 "Read one text file relative to the workspace.", 'rel_path="notes.txt"')
    reg.register("fs.write", fs_write(workspace), WRITE,
                 "Create or overwrite one text file relative to the workspace.", 'rel_path="out.txt", text="..."')
    reg.register("shell", shell_run(workspace), WRITE,
                 "Run one shell command with cwd pinned to the workspace.", 'command="dir"')
    return reg
