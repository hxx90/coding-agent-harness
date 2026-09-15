"""Immutable source/dependency bundles and an isolated Python runtime snapshot."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path, PurePosixPath

from agent_harness.coding.types import CodingError, Json
from agent_harness.sandbox import python_launcher_rules

from .store import Store, digest


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_bundle(root: Path, manifest_path: Path) -> Json:
    """Read an explicit file allowlist once, including vendored dependencies."""
    root = root.resolve()
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files", [])
    if not isinstance(files, list) or not 1 <= len(files) <= 100:
        raise CodingError(
            "invalid_program", "Manifest needs 1..100 explicit Python files"
        )
    sources: Json = {}
    for name in files:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or p.suffix != ".py" or str(p) != name:
            raise CodingError(
                "invalid_program", "Only canonical relative .py paths are supported"
            )
        path = root / name
        if not path.resolve().is_relative_to(root) or any(
            x.is_symlink()
            for x in [path, *path.parents]
            if x != root and x.is_relative_to(root)
        ):
            raise CodingError(
                "invalid_program", "Symlinked program sources are not supported"
            )
        sources[name] = path.read_text()
    if len(json.dumps(sources)) > 1024 * 1024:
        raise CodingError("invalid_program", "Program sources exceed 1 MiB")
    return {"manifest": manifest, "sources": sources}


class Programs:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.runtime: Json | None = None

    def _runtime(self) -> Json:
        if self.runtime is not None:
            return self.runtime
        # Freeze Python library code, native extension modules and Robo's SDK.
        # The interpreter image is pinned and rechecked at every launch. OS
        # libraries remain platform dependencies, as with an ordinary container.
        library = Path(sysconfig.get_path("stdlib"))
        worker = Path(__file__).with_name("worker.py").read_text()
        sources = sorted(
            p
            for p in library.rglob("*.py")
            if not any(
                part
                in {
                    "site-packages",
                    "__pycache__",
                    "test",
                    "tests",
                    "idlelib",
                    "tkinter",
                    "turtledemo",
                    "ensurepip",
                }
                for part in p.relative_to(library).parts
            )
        )
        native = sorted(Path(sysconfig.get_config_var("DESTSHARED")).glob("*.so"))
        hashes = {
            str(p.relative_to(library)): file_hash(p) for p in [*sources, *native]
        }
        identity: Json = {
            "python": sys.version,
            "executable": str(
                Path(getattr(sys, "_base_executable", sys.executable)).resolve()
            ),
            "executable_sha256": file_hash(
                Path(getattr(sys, "_base_executable", sys.executable))
            ),
            "library": hashes,
            "worker_sha256": hashlib.sha256(worker.encode()).hexdigest(),
        }
        key = digest(identity)
        target = self.store.root / "runtimes" / key
        if not target.exists():
            temp = target.with_name(key + ".tmp")
            temp.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                temp / "stdlib.zip", "w", zipfile.ZIP_DEFLATED
            ) as archive:
                for path in sources:
                    archive.writestr(str(path.relative_to(library)), path.read_bytes())
            (temp / "native").mkdir(exist_ok=True)
            for path in native:
                shutil.copyfile(path, temp / "native" / path.name)
            (temp / "worker.py").write_text(worker)
            (temp / "identity.json").write_text(json.dumps(identity))
            temp.rename(target)
        self.runtime = {
            "id": key,
            "identity": identity,
            "path": str(target),
            "stdlib_sha256": file_hash(target / "stdlib.zip"),
        }
        return self.runtime

    def publish(self, bundle: Json) -> Json:
        manifest, sources = bundle["manifest"], bundle["sources"]
        if not isinstance(manifest, dict) or not isinstance(sources, dict):
            raise CodingError("invalid_program", "Expected manifest and source objects")
        entry = manifest.get("entrypoint")
        if entry not in sources or not str(entry).endswith(".py"):
            raise CodingError(
                "invalid_program", "Entrypoint must be a bundled Python file"
            )
        if (
            sorted(sources) != sorted(manifest.get("files", []))
            or len(json.dumps(sources)) > 1024 * 1024
        ):
            raise CodingError(
                "invalid_program", "Source files must match manifest and fit 1 MiB"
            )
        if manifest.get("dependencies", {}) != {}:
            raise CodingError(
                "unsupported_dependencies",
                "MVP supports stdlib and explicitly vendored .py files; no ambient/pip dependencies",
            )
        for name, source in sources.items():
            p = PurePosixPath(name)
            if (
                p.is_absolute()
                or ".." in p.parts
                or p.suffix != ".py"
                or str(p) != name
                or not isinstance(source, str)
            ):
                raise CodingError("invalid_program", "Invalid bundled source")
            compile(source, name, "exec")
        runtime = self._runtime()
        value = {"manifest": manifest, "sources": sources, "runtime": runtime}
        key = digest(value)
        program = {"id": key, **value}
        self.store.put("program", key, program, immutable=True)
        path = self.store.root / "programs" / (key + ".zip")
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            with zipfile.ZipFile(path, "w") as archive:
                for name, source in sorted(sources.items()):
                    archive.writestr(name, source)
            path.chmod(0o400)
        return {
            "id": key,
            "manifest": manifest,
            "runtime_id": runtime["id"],
            "source_hashes": {
                k: hashlib.sha256(v.encode()).hexdigest() for k, v in sources.items()
            },
        }

    def command(self, program: Json, payload: Json) -> list[str]:
        runtime = program["runtime"]
        root = Path(runtime["path"])
        identity = runtime["identity"]
        executable = Path(identity["executable"])
        if (
            file_hash(executable) != identity["executable_sha256"]
            or file_hash(root / "stdlib.zip") != runtime["stdlib_sha256"]
            or file_hash(root / "worker.py") != identity["worker_sha256"]
        ):
            raise CodingError(
                "runtime_changed",
                "Pinned Python runtime changed; publish a new version",
            )
        for name, sha in identity["library"].items():
            if (
                name.endswith(".so")
                and file_hash(root / "native" / Path(name).name) != sha
            ):
                raise CodingError("runtime_changed", "Pinned native dependency changed")
        archive_path = self.store.root / "programs" / (program["id"] + ".zip")
        with zipfile.ZipFile(archive_path) as archive:
            actual = {name: archive.read(name).decode() for name in archive.namelist()}
        if actual != program["sources"]:
            raise CodingError(
                "program_changed", "Program artifact integrity check failed"
            )
        payload.update(
            archive=str(archive_path),
            runtime=str(root),
            entrypoint=program["manifest"]["entrypoint"],
        )
        command = [
            str(executable),
            "-I",
            "-S",
            "-B",
            str(root / "worker.py"),
            json.dumps(payload),
        ]
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise CodingError(
                "sandbox_unavailable",
                "This MVP requires macOS sandbox-exec; Linux isolation has not been implemented",
            )
        framework = next(
            (p for p in executable.parents if p.name.endswith(".framework")),
            executable.parent.parent,
        )
        read_roots = [
            root,
            archive_path,
            framework,
            Path("/System"),
            Path("/usr/lib"),
            Path("/dev/null"),
            Path("/dev/urandom"),
            Path("/private/etc/localtime"),
        ]
        profile = (
            '(version 1)(deny default)(import "system.sb")(allow process-info*)(allow process-exec (literal '
            + json.dumps(str(executable))
            + "))(allow sysctl-read)(allow mach-lookup)"
        )
        profile += (
            "(allow file-read* "
            + " ".join("(subpath " + json.dumps(str(p)) + ")" for p in read_roots)
            + ")"
        )
        profile += '(allow file-read-metadata)(allow file-write* (literal "/dev/null"))'
        profile += python_launcher_rules(executable)
        profile += "(deny network*)(deny file-write*)(deny process-fork)"
        return ["/usr/bin/sandbox-exec", "-p", profile, *command]
