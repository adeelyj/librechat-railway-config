from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_SERVICE_PREFIX = "/services/bauer-evidence-v3/"
RAILWAY_ROOT_DIRECTORY = "/services/bauer-evidence-v3"
RAILWAY_CONFIG_PATHS = {
    "api": f"{RAILWAY_ROOT_DIRECTORY}/railway.api.json",
    "worker": f"{RAILWAY_ROOT_DIRECTORY}/railway.worker.json",
    "migrate": f"{RAILWAY_ROOT_DIRECTORY}/railway.migrate.json",
}

_DIRECT_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==(?P<version>[^\s;]+)$"
)
_LOCK_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s;\\]+)"
    r"(?:\s*;.*)?\s*\\?$"
)
_PINNED_BASE = re.compile(
    r"^FROM "
    r"python:3\.11\.15-slim-bookworm"
    r"@sha256:(?P<digest>[0-9a-f]{64})$",
    re.MULTILINE,
)


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _direct_requirements(path: Path) -> set[tuple[str, str]]:
    pins: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DIRECT_PIN.fullmatch(line)
        if match is None:
            raise AssertionError(
                f"{path.name}:{line_number} must be an exact name==version pin"
            )
        pins.add(
            (
                _canonical_package_name(match.group("name")),
                match.group("version"),
            )
        )
    return pins


def _locked_requirements(path: Path) -> set[tuple[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        match = _LOCK_PIN.fullmatch(line)
        if match is None:
            raise AssertionError(
                f"{path.name}:{index + 1} is not a fully pinned lock entry"
            )
        starts.append((index, match))

    if not starts:
        raise AssertionError(f"{path.name} contains no locked requirements")

    pins: set[tuple[str, str]] = set()
    for position, (start, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        if "--hash=sha256:" not in block:
            raise AssertionError(
                f"{path.name}:{start + 1} lacks a sha256 distribution hash"
            )
        pins.add(
            (
                _canonical_package_name(match.group("name")),
                match.group("version"),
            )
        )
    return pins


class ContainerContractTests(unittest.TestCase):
    def test_images_pin_the_same_python_base_by_immutable_digest(self) -> None:
        matches = []
        for filename in ("Dockerfile.api", "Dockerfile.worker"):
            dockerfile = (SERVICE_ROOT / filename).read_text(encoding="utf-8")
            match = _PINNED_BASE.search(dockerfile)
            self.assertIsNotNone(match, filename)
            self.assertNotIn(":latest", dockerfile.casefold())
            matches.append(match.group("digest"))

        self.assertEqual(matches[0], matches[1])

    def test_images_install_hash_locked_dependencies_and_run_non_root(self) -> None:
        expected = {
            "Dockerfile.api": ("requirements-api.lock.txt", "api"),
            "Dockerfile.worker": ("requirements-worker.lock.txt", "worker"),
        }
        for filename, (lockfile, role) in expected.items():
            dockerfile = (SERVICE_ROOT / filename).read_text(encoding="utf-8")
            self.assertIn(f"COPY {lockfile} ./", dockerfile)
            self.assertIn("--only-binary=:all:", dockerfile)
            self.assertIn("--require-hashes", dockerfile)
            self.assertIn(f"--requirement {lockfile}", dockerfile)
            self.assertIn("python -m pip check", dockerfile)
            self.assertIn("COPY --chown=0:0 bauer_evidence_v3", dockerfile)
            self.assertIn("COPY --chown=0:0 migrations", dockerfile)
            self.assertIn("USER 10001:10001", dockerfile)
            self.assertNotIn("chown -R appuser", dockerfile)
            self.assertIn(
                f'CMD ["python", "-m", "bauer_evidence_v3.entrypoint", "{role}"]',
                dockerfile,
            )

    def test_api_and_worker_dependency_surfaces_are_separate(self) -> None:
        api_lock = (SERVICE_ROOT / "requirements-api.lock.txt").read_text(
            encoding="utf-8"
        )
        worker_lock = (SERVICE_ROOT / "requirements-worker.lock.txt").read_text(
            encoding="utf-8"
        )
        for worker_only in (
            "rapidocr-onnxruntime==",
            "onnxruntime==",
            "pdfplumber==",
            "pypdfium2==",
        ):
            self.assertNotIn(worker_only, api_lock)
            self.assertIn(worker_only, worker_lock)

    def test_worker_native_runtime_libraries_use_an_immutable_debian_snapshot(
        self,
    ) -> None:
        worker = (SERVICE_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        self.assertRegex(worker, r"ARG DEBIAN_SNAPSHOT=\d{8}T\d{6}Z")
        self.assertIn(
            "https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}",
            worker,
        )
        self.assertIn(
            "https://snapshot.debian.org/archive/debian-security/"
            "${DEBIAN_SNAPSHOT}",
            worker,
        )
        for package in ("libgl1", "libglib2.0-0", "libgomp1"):
            self.assertRegex(worker, rf"(?m)^\s+{re.escape(package)}(?:\s|\\)")
        self.assertIn("Acquire::Check-Valid-Until", worker)
        self.assertIn("rm -rf /var/lib/apt/lists/*", worker)

    def test_lockfiles_cover_every_declared_runtime_pin(self) -> None:
        manifests = {
            "api": (
                "requirements.txt",
                "requirements-observability.txt",
            ),
            "worker": (
                "requirements.txt",
                "requirements-parser.txt",
                "requirements-ocr.txt",
                "requirements-observability.txt",
            ),
        }
        for profile, filenames in manifests.items():
            declared: set[tuple[str, str]] = set()
            for filename in filenames:
                declared.update(_direct_requirements(SERVICE_ROOT / filename))
            locked = _locked_requirements(
                SERVICE_ROOT / f"requirements-{profile}.lock.txt"
            )
            self.assertTrue(
                declared.issubset(locked),
                f"{profile} lock is missing {sorted(declared - locked)}",
            )

        licensed = _direct_requirements(
            SERVICE_ROOT / "requirements-pymupdf-licensed.txt"
        )
        self.assertTrue(licensed)
        worker_locked = _locked_requirements(
            SERVICE_ROOT / "requirements-worker.lock.txt"
        )
        self.assertTrue(
            licensed.isdisjoint(worker_locked),
            "the separately licensed parser must not enter the production image",
        )


class RailwayContractTests(unittest.TestCase):
    """Validate config-as-code under the required service-relative dashboard root.

    Railway does not encode Root Directory or custom config-file selection inside
    railway.json. Each Railway service must therefore set Root Directory to
    ``/services/bauer-evidence-v3`` and select its absolute config path from
    ``RAILWAY_CONFIG_PATHS``.
    """

    def test_configs_are_service_relative_and_explicitly_role_bound(self) -> None:
        expected = {
            "api": {
                "dockerfile": "Dockerfile.api",
                "restart": "ALWAYS",
                "health": "/health",
                "lockfile": "requirements-api.lock.txt",
            },
            "worker": {
                "dockerfile": "Dockerfile.worker",
                "restart": "ALWAYS",
                "health": None,
                "lockfile": "requirements-worker.lock.txt",
            },
            "migrate": {
                "dockerfile": "Dockerfile.api",
                "restart": "NEVER",
                "health": None,
                "lockfile": "requirements-api.lock.txt",
            },
        }
        for role, contract in expected.items():
            config_path = SERVICE_ROOT / f"railway.{role}.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            build = config["build"]
            deploy = config["deploy"]

            self.assertEqual(
                config["$schema"],
                "https://railway.com/railway.schema.json",
            )
            self.assertEqual(build["builder"], "DOCKERFILE")
            self.assertEqual(build["dockerfilePath"], contract["dockerfile"])
            self.assertTrue((SERVICE_ROOT / contract["dockerfile"]).is_file())
            self.assertNotIn("/", build["dockerfilePath"])
            self.assertEqual(
                deploy["startCommand"],
                f"python -m bauer_evidence_v3.entrypoint {role}",
            )
            self.assertEqual(deploy["restartPolicyType"], contract["restart"])

            watch_patterns = build["watchPatterns"]
            self.assertTrue(watch_patterns)
            self.assertTrue(
                all(
                    pattern.startswith(REPOSITORY_SERVICE_PREFIX)
                    for pattern in watch_patterns
                )
            )
            self.assertIn(
                f"{REPOSITORY_SERVICE_PREFIX}{contract['dockerfile']}",
                watch_patterns,
            )
            self.assertIn(
                f"{REPOSITORY_SERVICE_PREFIX}{contract['lockfile']}",
                watch_patterns,
            )
            self.assertIn(
                f"{REPOSITORY_SERVICE_PREFIX}{config_path.name}",
                watch_patterns,
            )

            if contract["health"] is None:
                self.assertNotIn("healthcheckPath", deploy)
                self.assertNotIn("healthcheckTimeout", deploy)
            else:
                self.assertEqual(
                    deploy["healthcheckPath"],
                    contract["health"],
                )
                self.assertIsInstance(deploy["healthcheckTimeout"], int)

    def test_migrator_is_a_non_restarting_one_shot_not_an_api_or_worker(self) -> None:
        config = json.loads(
            (SERVICE_ROOT / "railway.migrate.json").read_text(encoding="utf-8")
        )
        deploy = config["deploy"]
        self.assertEqual(
            deploy["startCommand"],
            "python -m bauer_evidence_v3.entrypoint migrate",
        )
        self.assertEqual(deploy["restartPolicyType"], "NEVER")
        self.assertNotIn("preDeployCommand", deploy)
        self.assertNotIn("cronSchedule", deploy)
        self.assertNotIn("healthcheckPath", deploy)
        self.assertNotIn("healthcheckTimeout", deploy)

    def test_external_railway_path_contract_is_unambiguous(self) -> None:
        self.assertEqual(
            RAILWAY_ROOT_DIRECTORY,
            "/services/bauer-evidence-v3",
        )
        self.assertEqual(
            set(RAILWAY_CONFIG_PATHS),
            {"api", "worker", "migrate"},
        )
        for role, path in RAILWAY_CONFIG_PATHS.items():
            self.assertEqual(
                path,
                f"{RAILWAY_ROOT_DIRECTORY}/railway.{role}.json",
            )


if __name__ == "__main__":
    unittest.main()
