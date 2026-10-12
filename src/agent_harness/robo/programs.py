"""Immutable source/dependency bundles and an isolated Python runtime snapshot."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import sys
import sysconfig
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from agent_harness.coding.types import CodingError, Json
from agent_harness.sandbox import python_launcher_rules

from .store import Store, digest


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_runtime_files(files: list[tuple[Path, Path]]) -> None:
    """Bound independent file I/O; return only once the complete copy succeeds."""
    if not files:
        return
    for directory in sorted({destination.parent for _, destination in files}):
        directory.mkdir(parents=True, exist_ok=True)
    sources, destinations = zip(*files)
    with ThreadPoolExecutor(max_workers=8) as pool:
        # Consume the iterator so copy errors propagate before atomic publication.
        list(pool.map(shutil.copyfile, sources, destinations))


def parameter_ranges(value: object) -> dict[str, tuple[float, float]]:
    """Validate declared bounds before freezing code or admitting a job."""
    if not isinstance(value, dict):
        raise CodingError("invalid_program", "Manifest parameters must be an object")
    result = {}
    for name, spec in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(spec, dict)
            or not {"min", "max"}.issubset(spec)
            or any(
                isinstance(n, bool) or not isinstance(n, (int, float))
                for n in (spec["min"], spec["max"])
            )
            or not -1e6 <= spec["min"] <= spec["max"] <= 1e6
        ):
            raise CodingError(
                "invalid_program",
                "Each manifest parameter needs finite numeric bounds "
                "{'min': lower, 'max': upper}, with -1e6 <= lower <= upper <= 1e6; "
                "pass parameter values to program_run, not here",
            )
        result[name] = (spec["min"], spec["max"])
    return result


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
    def __init__(self, store: Store, *, packages: tuple[str, ...] = ()) -> None:
        self.store = store
        self.packages = {
            name: importlib.metadata.distribution(name) for name in packages
        }
        self.runtimes: dict[str, Json] = {}

    def dependencies(self) -> dict[str, str]:
        return {
            name: distribution.version for name, distribution in self.packages.items()
        }

    def _runtime(self, dependencies: dict[str, str]) -> Json:
        versions = self.dependencies()
        if any(
            name not in versions or version not in {"*", versions[name]}
            for name, version in dependencies.items()
        ):
            raise CodingError(
                "unsupported_dependencies",
                "Request only host-approved installed versions: " + str(versions),
            )
        selected = {name: versions[name] for name in sorted(dependencies)}
        selection_key = digest(selected)
        if selection_key in self.runtimes:
            return self.runtimes[selection_key]
        # Freeze Python library code, native extension modules and Robo's SDK.
        # The interpreter image is pinned and rechecked at every launch. OS
        # libraries remain platform dependencies, as with an ordinary container.
        library = Path(sysconfig.get_path("stdlib"))
        worker = Path(__file__).with_name("worker.py").read_text()
        linux = Path(__file__).with_name("linux.py").read_text()
        wire = Path(__file__).with_name("wire.py").read_text()
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
        # Relocatable Python installations may retain a build-time DESTSHARED.
        native_root = library / "lib-dynload"
        if not native_root.is_dir():
            native_root = Path(sysconfig.get_config_var("DESTSHARED"))
        native = sorted(native_root.glob("*.so"))
        hashes = {
            str(p.relative_to(library)): file_hash(p) for p in [*sources, *native]
        }
        package_files: dict[str, Path] = {}
        for name in selected:
            distribution = self.packages[name]
            base = distribution.locate_file("").resolve()
            for entry in distribution.files or ():
                relative = PurePosixPath(str(entry))
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "__pycache__" in relative.parts
                    or relative.suffix == ".pyc"
                ):
                    continue
                path = distribution.locate_file(entry)
                if not path.is_file() or not path.resolve().is_relative_to(base):
                    raise CodingError(
                        "invalid_dependency", "Package has missing or external files"
                    )
                package_files[str(relative)] = path
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
            "linux_sha256": hashlib.sha256(linux.encode()).hexdigest(),
            "wire_sha256": hashlib.sha256(wire.encode()).hexdigest(),
            "dependencies": selected,
            "package_files": {
                name: file_hash(path) for name, path in package_files.items()
            },
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
                archive.writestr("_robo_linux.py", linux)
                archive.writestr("_robo_wire.py", wire)
            (temp / "native").mkdir(exist_ok=True)
            copy_runtime_files(
                [(path, temp / "native" / path.name) for path in native]
                + [
                    (path, temp / "packages" / name)
                    for name, path in package_files.items()
                ]
            )
            (temp / "worker.py").write_text(worker)
            (temp / "identity.json").write_text(json.dumps(identity))
            temp.rename(target)
        runtime = {
            "id": key,
            "identity": identity,
            "path": str(target),
            "stdlib_sha256": file_hash(target / "stdlib.zip"),
        }
        self.runtimes[selection_key] = runtime
        return runtime

    def publish(self, bundle: Json) -> Json:
        manifest, sources = bundle["manifest"], bundle["sources"]
        if not isinstance(manifest, dict) or not isinstance(sources, dict):
            raise CodingError("invalid_program", "Expected manifest and source objects")
        parameter_ranges(manifest.get("parameters", {}))
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
        dependencies = manifest.get("dependencies", {})
        if not isinstance(dependencies, dict) or not all(
            isinstance(name, str) and isinstance(version, str)
            for name, version in dependencies.items()
        ):
            raise CodingError(
                "unsupported_dependencies",
                "Dependencies must map host-approved package names to installed versions",
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
        runtime = self._runtime(dependencies)
        value = {"manifest": manifest, "sources": sources, "runtime": runtime}
        key = digest(value)
        program = {"id": key, **value}
        path = self.store.root / "programs" / (key + ".zip")
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            with zipfile.ZipFile(temporary, "w") as archive:
                for name, source in sorted(sources.items()):
                    archive.writestr(name, source)
            temporary.replace(path)
            path.chmod(0o400)
        self.store.put("program", key, program, immutable=True)
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
        for name, sha in identity.get("package_files", {}).items():
            if file_hash(root / "packages" / name) != sha:
                raise CodingError(
                    "runtime_changed", "Pinned package dependency changed"
                )
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
        if sys.platform == "linux":
            # The frozen worker installs Landlock/seccomp before user code.
            # Failure exits the worker, never silently executes without isolation.
            return command
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise CodingError(
                "sandbox_unavailable",
                "Robo requires macOS sandbox-exec or Linux Landlock and libseccomp",
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
