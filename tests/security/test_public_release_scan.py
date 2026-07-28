from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = REPOSITORY_ROOT / "scripts" / "public_release_scan.py"


def _load_scanner() -> ModuleType:
    specification = importlib.util.spec_from_file_location("public_release_scan", SCANNER_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("scanner import failed")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


SCANNER = _load_scanner()


def _write_safe_repository(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "data" / "raw").mkdir(parents=True)
    marker = {
        "history_policy": "sanitized-working-tree-new-root",
        "private_history_imported": False,
        "repository_name": "adaptive-llm-quant-public",
        "schema_version": "public-release-root.v1",
    }
    (root / "public-release-root.json").write_text(
        json.dumps(marker, sort_keys=True),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "\n".join(
            (
                ".env",
                ".env.*",
                "!.env.example",
                ".local/",
                "config/paper-account.yaml",
                "data/raw/**",
                "browser-profile/",
                "chrome-profile/",
                "user-data-dir/",
                "*.pem",
                "*.key",
            )
        ),
        encoding="utf-8",
    )
    (root / "data" / "raw" / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    (root / "config" / "paper-account.example.yaml").write_text(
        "\n".join(
            (
                'schema_version: "paper-account.v1"',
                'account_id: "synthetic-paper-account"',
                'source: "SYNTHETIC_EXAMPLE"',
                "cash:",
                '  - currency: "USD"',
                '    amount: "100000.00"',
                '  - currency: "KRW"',
                '    amount: "0"',
                "positions:",
                '  - symbol: "SPY"',
                '    quantity: "11"',
                '  - symbol: "QQQ"',
                '    quantity: "7"',
                '  - symbol: "IWM"',
                '    quantity: "13"',
                '  - symbol: "SMH"',
                '    quantity: "9"',
                '  - symbol: "TLT"',
                '    quantity: "17"',
                '  - symbol: "HYG"',
                '    quantity: "19"',
                '  - symbol: "GLD"',
                '    quantity: "6"',
            )
        ),
        encoding="utf-8",
    )
    (root / "safe.txt").write_text("public synthetic fixture\n", encoding="utf-8")


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError("test git command failed")


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Public Test",
        "-c",
        "user.email=public-test@example.com",
        "commit",
        "-m",
        message,
    )


class PublicReleaseScannerTests(unittest.TestCase):
    def test_safe_worktree_passes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)

            violations = SCANNER.scan_repository(root, require_git=False)

            self.assertEqual(violations, [])

    def test_secret_email_account_and_private_path_are_redacted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            sensitive_token = "gh" + "p_" + ("A" * 40)
            entropy_token = (
                "aB3dE5fG7hJ9kL2mN4pQ6rS8"
                + "tU1vW3xY5zA7cD9eF2gH4jK6"
            )
            private_email = "person" + chr(64) + "private.invalidtld"
            private_path = "C" + ":\\" + "Users\\" + "private-user\\profile"
            account_line = "account_" + "id: brokerage-123456"
            payload = "\n".join(
                (
                    sensitive_token,
                    entropy_token,
                    private_email,
                    private_path,
                    account_line,
                )
            )
            (root / "unsafe.txt").write_text(payload, encoding="utf-8")

            violations = SCANNER.scan_repository(root, require_git=False)
            rules = {violation.rule for violation in violations}
            report = SCANNER.format_report(violations)

            self.assertGreaterEqual(
                rules,
                {
                    "GITHUB_TOKEN",
                    "HIGH_ENTROPY_TOKEN",
                    "PERSONAL_EMAIL",
                    "PRIVATE_ABSOLUTE_PATH",
                    "NON_SYNTHETIC_ACCOUNT_ID",
                },
            )
            self.assertNotIn(sensitive_token, report)
            self.assertNotIn(private_email, report)
            self.assertNotIn(private_path, report)
            self.assertNotIn(entropy_token, report)

    def test_sensitive_filename_is_redacted_from_report(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            sensitive_token = "gh" + "p_" + ("C" * 40)
            filename = "cookie-" + sensitive_token + ".json"
            (root / filename).write_text("{}", encoding="utf-8")

            violations = SCANNER.scan_repository(root, require_git=False)
            report = SCANNER.format_report(violations)

            self.assertIn("BROWSER_CREDENTIAL_FILE", {item.rule for item in violations})
            self.assertNotIn(sensitive_token, report)
            self.assertIn("[redacted-path:", report)

    def test_diff_decorator_is_not_an_email_but_real_diff_email_is_detected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            private_email = "person" + chr(64) + "private.invalidtld"
            (root / "candidate.patch").write_text(
                "+" + chr(64) + "pytest.mark.parametrize(\n"
                f"+owner = {private_email}\n",
                encoding="utf-8",
            )

            violations = SCANNER.scan_repository(root, require_git=False)

            email_findings = [
                item for item in violations if item.rule == "PERSONAL_EMAIL"
            ]
            self.assertEqual(len(email_findings), 1)
            self.assertEqual(email_findings[0].line, 2)

    def test_local_artifacts_binary_and_lfs_pointer_fail(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            (root / ".local").mkdir()
            (root / ".local" / "state.json").write_text("{}", encoding="utf-8")
            (root / "capture.bin").write_bytes(b"\x00\x01")
            lfs_pointer = "\n".join(
                (
                    "version https://" + "git-lfs.github.com/spec/v1",
                    "oid sha256:" + ("a" * 64),
                    "size 42",
                )
            )
            (root / "large.txt").write_text(lfs_pointer, encoding="utf-8")

            rules = {
                violation.rule
                for violation in SCANNER.scan_repository(root, require_git=False)
            }

            self.assertGreaterEqual(
                rules,
                {"FORBIDDEN_LOCAL_PATH", "BINARY_FILE", "GIT_LFS_POINTER"},
            )

    def test_clean_root_marker_must_exist_in_first_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            marker = root / "public-release-root.json"
            marker.unlink()
            _git(root, "init")
            _commit_all(root, "unmarked root")
            marker_payload = {
                "history_policy": "sanitized-working-tree-new-root",
                "private_history_imported": False,
                "repository_name": "adaptive-llm-quant-public",
                "schema_version": "public-release-root.v1",
            }
            marker.write_text(json.dumps(marker_payload), encoding="utf-8")
            _commit_all(root, "late marker")

            rules = {
                violation.rule
                for violation in SCANNER.scan_git_history(
                    root, "story7077/adaptive-llm-quant-public"
                )
            }

            self.assertIn("CLEAN_ROOT_MARKER", rules)

    def test_deleted_secret_remains_blocked_by_history_scan(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            historical_token = "gh" + "p_" + ("B" * 40)
            (root / "temporary.txt").write_text(historical_token, encoding="utf-8")
            _git(root, "init")
            _commit_all(root, "marked root")
            (root / "temporary.txt").unlink()
            _commit_all(root, "remove unsafe file")

            violations = SCANNER.scan_repository(
                root,
                expected_repository="story7077/adaptive-llm-quant-public",
            )
            report = SCANNER.format_report(violations)

            self.assertIn("GITHUB_TOKEN", {item.rule for item in violations})
            self.assertNotIn(historical_token, report)

    def test_valid_marked_clean_root_passes_history_scan(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            _git(root, "init")
            _commit_all(root, "public foundation")

            violations = SCANNER.scan_repository(
                root,
                expected_repository="story7077/adaptive-llm-quant-public",
            )

            self.assertEqual(violations, [])

    def test_non_example_commit_email_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            _git(root, "init")
            _git(root, "add", "-A")
            private_email = "person" + chr(64) + "private.invalidtld"
            _git(
                root,
                "-c",
                "user.name=Private Test",
                "-c",
                f"user.email={private_email}",
                "commit",
                "-m",
                "public foundation",
            )

            violations = SCANNER.scan_repository(
                root,
                expected_repository="story7077/adaptive-llm-quant-public",
            )
            report = SCANNER.format_report(violations)

            self.assertIn("PERSONAL_EMAIL", {item.rule for item in violations})
            self.assertNotIn(private_email, report)

    def test_github_pull_request_merge_identity_and_signature_are_not_content(self) -> None:
        private_email = "person" + chr(64) + "private.invalidtld"
        github_noreply = "noreply" + chr(64) + "github.com"
        commit_text = "\n".join(
            (
                "tree " + ("a" * 40),
                "parent " + ("b" * 40),
                "parent " + ("c" * 40),
                f"author Public User <{private_email}> 1 +0000",
                f"committer GitHub <{github_noreply}> 1 +0000",
                "gpgsig -----BEGIN PGP SIGNATURE-----",
                " "
                + (
                    "A1b2C3d4E5f6G7h8I9j0"
                    + "K1l2M3n4O5p6Q7r8S9t0"
                )
                * 2,
                " -----END PGP SIGNATURE-----",
                "",
                "Merge synthetic pull request",
            )
        )

        sanitized = SCANNER._commit_text_for_scan(commit_text)
        violations = SCANNER._scan_text(".git/commits/synthetic", sanitized)

        self.assertEqual(violations, [])
        self.assertIn("Merge synthetic pull request", sanitized)
        self.assertNotIn(private_email, sanitized)

        unsafe_message = sanitized + "\n" + "gh" + "p_" + ("Z" * 40)
        unsafe = SCANNER._scan_text(".git/commits/synthetic", unsafe_message)
        self.assertIn("GITHUB_TOKEN", {item.rule for item in unsafe})

    def test_ignored_local_secret_is_excluded_but_force_added_secret_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_safe_repository(root)
            _git(root, "init")
            _commit_all(root, "public foundation")
            local_secret_path = root / ".env"
            local_secret = (
                "aB3dE5fG7hJ9kL2mN4pQ6rS8"
                + "tU1vW3xY5zA7cD9eF2gH4jK6"
            )
            local_secret_path.write_text(
                "PRIVATE_" + "TOKEN=" + local_secret,
                encoding="utf-8",
            )

            ignored_violations = SCANNER.scan_repository(
                root,
                expected_repository="story7077/adaptive-llm-quant-public",
            )
            self.assertEqual(ignored_violations, [])

            _git(root, "add", "-f", ".env")
            forced_violations = SCANNER.scan_repository(
                root,
                expected_repository="story7077/adaptive-llm-quant-public",
            )
            self.assertIn("FORBIDDEN_DOTENV", {item.rule for item in forced_violations})


if __name__ == "__main__":
    unittest.main()
