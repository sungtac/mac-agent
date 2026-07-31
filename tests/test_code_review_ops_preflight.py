import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("code_review_ops_preflight", ROOT / "bin" / "code-review-ops-preflight.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class CodeReviewOpsPreflightTests(unittest.TestCase):
    def test_repo_and_plist_configuration_are_structurally_valid(self):
        result = MODULE.preflight(ROOT / "config" / "code-review-repositories.json", ROOT / "config" / "com.macagent.code-review-worker.plist.template", allow_execute=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["checks"]["plist_has_execute"])
        self.assertIn("sungtac/mac-agent", result["checks"]["repositories"])

    def test_missing_repo_is_an_error_without_touching_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            plist = root / "worker.plist"
            config.write_text(json.dumps({
                "schema": MODULE.CONFIG_SCHEMA,
                "repositories": {"acme/widget": {"repository_root": str(root / "missing")}},
            }), encoding="utf-8")
            plist.write_bytes(plistlib.dumps({
                "Label": "com.macagent.code-review-worker",
                "ProgramArguments": ["node", "worker.js"],
            }))
            result = MODULE.preflight(config, plist, allow_execute=True)
            self.assertFalse(result["ok"])
            self.assertTrue(any("does not exist" in message for message in result["errors"]))
            self.assertFalse((root / "missing").exists())

    def test_require_clean_promotes_dirty_worktree_to_error(self):
        result = MODULE.preflight(ROOT / "config" / "code-review-repositories.json", ROOT / "config" / "com.macagent.code-review-worker.plist.template", require_clean=True, allow_execute=True)
        # The shared worktree may be clean or may contain the user's pending changes.
        if result["checks"]["repositories"]["sungtac/mac-agent"]["clean"]:
            self.assertTrue(result["ok"], result)
        else:
            self.assertFalse(result["ok"])
            self.assertTrue(any("dirty" in message for message in result["errors"]))


if __name__ == "__main__":
    unittest.main()
