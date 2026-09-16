"""Run: python3 -B -m unittest discover -s scripts/agent -p 'test_*.py' -v"""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import selectors
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

import install
import runtime

CHECKOUT = Path(__file__).resolve().parents[2]
BINARY = next((p for p in (CHECKOUT / "target/debug/tgrep", CHECKOUT / "target/release/tgrep") if p.exists()), None)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tgrep agent 'tests ")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.base = self.project / ".tgrep-agent"
        self.fake = self.root / "fake-tgrep"
        self.fake.write_text('#!/bin/sh\nprintf "%s\\n" "tgrep test --no-index --index-path --json --files"\n')
        self.fake.chmod(0o755)
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache"), "XDG_DATA_HOME": str(self.root / "data")})
        self.environment.start()
        self.homedir = patch.object(Path, "home", return_value=self.root / "home")
        self.homedir.start()

    def tearDown(self):
        self.homedir.stop()
        self.environment.stop()
        self.temp.cleanup()

    def run_install(self, action="install", agent="codex,pi", extra=()):
        argv = ["install.py", action, "--agent", agent, "--root", str(self.project), "--binary", str(self.fake), *extra]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            return install.main()

    def test_install_reinstall_partial_uninstall_preserves_user_configuration(self):
        codex = self.project / ".codex"
        codex.mkdir()
        original = b'# user settings\nmodel = "my-model"\n[mcp_servers.other]\ncommand = "other"\n'
        (codex / "config.toml").write_bytes(original)
        old_hook = {"hooks": [{"type": "command", "command": "echo existing"}]}
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [old_hook]}, "description": "mine"}))
        (self.project / "AGENTS.md").write_bytes(b"Existing instructions without trailing newline")
        self.run_install()
        self.run_install()
        parsed = tomllib.loads((codex / "config.toml").read_text())
        self.assertEqual(set(parsed["mcp_servers"]), {"other", "tgrep"})
        self.assertEqual(len(json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"]), 2)
        # Changes outside owned sections must survive uninstall.
        with (codex / "config.toml").open("a") as stream:
            stream.write('\n[user_extra]\nvalue = "keep"\n')
        self.run_install("uninstall", "codex")
        self.assertTrue((self.project / ".pi/extensions/tgrep.ts").exists())
        self.assertEqual((codex / "config.toml").read_bytes(), original + b'\n[user_extra]\nvalue = "keep"\n')
        self.assertEqual(json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"], [old_hook])
        self.assertEqual((self.project / "AGENTS.md").read_bytes(), b"Existing instructions without trailing newline")
        self.run_install("uninstall", "pi")
        self.assertFalse((self.project / ".pi/extensions/tgrep.ts").exists())
        self.assertEqual(json.loads((self.base / "manifest.json").read_text())["agents"], {})

    def test_inline_hooks_are_preserved(self):
        path = self.project / ".codex/config.toml"
        path.parent.mkdir()
        original = b'[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype="command"\ncommand="echo hello"\n'
        path.write_bytes(original)
        self.run_install(agent="codex")
        self.assertEqual(len(tomllib.loads(path.read_text())["hooks"]["SessionStart"]), 2)
        self.assertFalse((path.parent / "hooks.json").exists())
        self.run_install("uninstall", "codex")
        self.assertEqual(path.read_bytes(), original)

    def test_modified_file_prevents_partial_uninstall(self):
        self.run_install()
        extension = self.project / ".pi/extensions/tgrep.ts"
        extension.write_text(extension.read_text() + "\n// user change\n")
        before = (self.project / ".codex/config.toml").read_bytes()
        with self.assertRaisesRegex(ValueError, "modified"):
            self.run_install("uninstall")
        self.assertEqual((self.project / ".codex/config.toml").read_bytes(), before)
        self.assertIn("user change", extension.read_text())

    def test_conflict_has_no_partial_configuration_writes(self):
        path = self.project / ".pi/extensions/tgrep.ts"
        path.parent.mkdir(parents=True)
        path.write_text("user extension")
        with self.assertRaisesRegex(ValueError, "Unmanaged"):
            self.run_install()
        self.assertFalse((self.project / ".codex/config.toml").exists())
        self.assertFalse((self.base / "codex/runtime.py").exists())
        self.assertEqual(path.read_text(), "user extension")

    def test_repair_missing_runtime_preserves_options(self):
        self.run_install(extra=["--no-require-git", "--max-filesize", "8M"])
        (self.base / "codex/runtime.py").unlink()
        self.run_install("repair")
        data = json.loads((self.base / "codex/config.json").read_text())
        self.assertEqual(data["index_flags"], ["--no-require-git", "--max-filesize", "8M"])
        self.assertTrue((self.base / "codex/runtime.py").exists())

    def test_user_scope_resolves_session_repository(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-config"), "PI_CODING_AGENT_DIR": str(self.root / "pi-config")}):
            self.run_install(extra=["--scope", "user"])
            self.assertTrue((self.root / "codex-config/config.toml").exists())
            self.assertTrue((self.root / "pi-config/extensions/tgrep.ts").exists())
            config = json.loads((self.root / "data/tgrep-agent/codex/config.json").read_text())
            self.assertIsNone(config["root"])
            self.assertEqual(runtime.Runtime(config, self.project).root, self.project)
            self.run_install("uninstall", extra=["--scope", "user"])

    def test_commit_rolls_back_on_write_failure(self):
        first, second = self.root / "one", self.root / "two"
        first.write_bytes(b"original")
        transaction = install.Changes()
        transaction.set(first, b"changed")
        transaction.set(second, b"new")
        actual = install.atomic
        def fail(path, value):
            if path == second:
                raise OSError("simulated disk failure")
            actual(path, value)
        with patch.object(install, "atomic", side_effect=fail):
            with self.assertRaises(OSError):
                transaction.commit(self.base)
        self.assertEqual(first.read_bytes(), b"original")
        self.assertFalse(second.exists())


@unittest.skipUnless(BINARY, "Build tgrep before running runtime integration tests")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tgrep runtime ")
        self.directory = Path(self.temp.name)
        self.root = self.directory / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "src").mkdir()
        (self.root / "src/main.rs").write_text("fn needle() {}\nneedle();\n")
        (self.root / "plain.txt").write_text("needle\n")
        (self.root / ".hidden").write_text("hidden_needle\n")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        (self.root / "ignored.txt").write_text("needle\n")
        self.config = {"root": str(self.root), "binary": str(BINARY), "cache_dir": str(self.directory / "cache"), "index_flags": []}
        self.runtime = runtime.Runtime(self.config)

    def tearDown(self):
        for info in self.directory.glob("cache/*/index/serve.json"):
            try:
                os.kill(json.loads(info.read_text())["pid"], signal.SIGTERM)
            except (ProcessLookupError, FileNotFoundError):
                pass
        time.sleep(0.05)
        self.temp.cleanup()

    def search(self, tool="search_code", **args):
        return self.runtime.search(tool, {"freshness": "current", **args})["structuredContent"]

    def test_current_search_filters_and_scoped_paths(self):
        result = self.search(pattern="needle", file_types=["rust"], path="src", context_lines=0)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["path"], "src/main.rs")
        self.assertEqual(result["search_mode"], "current_scan")
        self.assertFalse(result["truncated"])
        files = self.search("find_files", pattern="*.rs")
        self.assertEqual(files["results"], [{"path": "src/main.rs"}])
        found = self.search("find_files", pattern="*", path="src")
        self.assertEqual(found["results"], [{"path": "src/main.rs"}])
        empty = self.search(pattern="nonexistent_token")
        self.assertEqual(empty["results"], [])
        hidden = self.search(pattern="hidden_needle", hidden=True)
        self.assertEqual(hidden["results"][0]["path"], ".hidden")
        self.assertFalse(any(r["path"] == "ignored.txt" for r in self.search(pattern="needle")["results"]))

    def test_limits_literal_flags_and_errors(self):
        result = self.search(pattern="needle", context_lines=0, max_results=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["results"]), 1)
        (self.root / "flags.txt").write_text("--help\nserve\n")
        self.assertEqual(len(self.search(pattern="--help", context_lines=0)["results"]), 1)
        self.assertEqual(len(self.search(pattern="serve", context_lines=0)["results"]), 1)
        with self.assertRaises(ValueError):
            self.search(pattern="[", literal=False)
        with self.assertRaisesRegex(ValueError, "range"):
            self.search(pattern="x", max_results=0)

    def test_path_escape_and_symlink_rejected(self):
        outside = self.directory / "outside.txt"
        outside.write_text("secret")
        (self.root / "link").symlink_to(outside)
        for path in ("../outside.txt", str(outside), "link"):
            with self.assertRaisesRegex(ValueError, "beneath"):
                self.search(pattern="secret", path=path)

    def test_server_start_reuse_and_latest_edit(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.runtime.ensure_server(wait=3), range(4)))
        deadline = time.monotonic() + 5
        while self.runtime.status() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.runtime.status())
        first = json.loads((self.runtime.index / "serve.json").read_text())
        self.assertTrue(self.runtime.ensure_server())
        self.assertEqual(first, json.loads((self.runtime.index / "serve.json").read_text()))
        result = self.runtime.search("search_code", {"pattern": "needle", "path": "src"})["structuredContent"]
        self.assertTrue(result["results"])
        (self.root / "new.txt").write_text("just_created_token")
        self.assertEqual(self.search(pattern="just_created_token")["results"][0]["path"], "new.txt")

    def test_failed_start_scans_instead_of_using_old_index(self):
        with patch.object(self.runtime, "ensure_server", return_value=False):
            result = self.runtime.search("search_code", {"pattern": "needle"})["structuredContent"]
        self.assertEqual(result["search_mode"], "current_scan")
        self.assertTrue(result["results"])

    def test_doctor_exercises_installed_mcp(self):
        with patch.object(sys, "argv", ["install.py", "install", "--agent", "codex", "--root", str(self.root), "--binary", str(BINARY)]), contextlib.redirect_stdout(io.StringIO()):
            install.main()
        manifest = json.loads((self.root / ".tgrep-agent/manifest.json").read_text())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(install.doctor(manifest, ["codex"], self.root), 0, output.getvalue())

    @unittest.skipUnless(shutil.which("node"), "Node required for pi bridge contract test")
    def test_pi_bridge_real_mcp_roundtrip(self):
        node = shutil.which("node")
        major = int(subprocess.check_output([node, "--version"], text=True).lstrip("v").split(".")[0])
        if major < 22:
            self.skipTest("Node 22+ required for built-in TypeScript stripping")
        with patch.object(sys, "argv", ["install.py", "install", "--agent", "pi", "--root", str(self.root), "--binary", str(BINARY)]), contextlib.redirect_stdout(io.StringIO()):
            install.main()
        result = subprocess.run([node, "--experimental-strip-types", str(Path(__file__).with_name("test_pi.mjs")),
                                 str(self.root / ".pi/extensions/tgrep.ts"), str(self.root)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_service_restarts_after_exit(self):
        self.assertTrue(self.runtime.ensure_server(wait=3))
        info = json.loads((self.runtime.index / "serve.json").read_text())
        os.kill(info["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while self.runtime.status() is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        result = self.runtime.search("search_code", {"pattern": "needle"})
        self.assertTrue(result["structuredContent"]["results"])
        replacement = json.loads((self.runtime.index / "serve.json").read_text())
        self.assertNotEqual(info["pid"], replacement["pid"])

    def test_mcp_cancellation_and_shutdown_reap_query_children(self):
        fake = self.directory / "slow-query"
        pid_file = self.directory / "query.pid"
        fake.write_text(f"#!{sys.executable}\nimport os, time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n")
        fake.chmod(0o755)
        config = self.directory / "slow-config.json"
        config.write_text(json.dumps({**self.config, "binary": str(fake)}))
        for shutdown in (False, True):
            pid_file.unlink(missing_ok=True)
            proc = subprocess.Popen([sys.executable, str(Path(runtime.__file__)), "mcp", "--config", str(config)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                def send(message):
                    proc.stdin.write(json.dumps(message) + "\n")
                    proc.stdin.flush()
                def receive():
                    with selectors.DefaultSelector() as selector:
                        selector.register(proc.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(5), "MCP response timed out")
                    return json.loads(proc.stdout.readline())
                send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
                self.assertIn("result", receive())
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_code", "arguments": {"pattern": "needle", "freshness": "current"}}})
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                pid = int(pid_file.read_text())
                if shutdown:
                    proc.terminate()
                    proc.wait(timeout=5)
                else:
                    send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
                    result = receive()["result"]
                    self.assertTrue(result["isError"])
                    self.assertIn("cancelled", result["content"][0]["text"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
