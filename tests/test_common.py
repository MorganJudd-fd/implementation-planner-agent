import os
import unittest

import helpers  # noqa: F401  (sets sys.path)
from plannerlib.common import PlannerError, check_repo_path, glob_match, require_key, validate


class TestKey(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(require_key("PROJ-1"), "PROJ-1")

    def test_traversal_rejected(self):
        for bad in ("../x", "proj-1", "PROJ-1/../../a", "PROJ-", ""):
            with self.assertRaises(PlannerError):
                require_key(bad)


class TestPaths(unittest.TestCase):
    def test_rules(self):
        with helpers.Fixture() as fx:
            self.assertIsNone(check_repo_path(fx.repo, "src/pricing/service.ts"))
            self.assertIn("absolute", check_repo_path(fx.repo, "/etc/passwd"))
            self.assertIn("..", check_repo_path(fx.repo, "src/../../x"))
            self.assertIn(".git", check_repo_path(fx.repo, ".git/config"))
            os.symlink("/etc", os.path.join(fx.repo, "escape"))
            self.assertIn("outside", check_repo_path(fx.repo, "escape/passwd"))


class TestGlob(unittest.TestCase):
    def test_double_star(self):
        self.assertTrue(glob_match("pnpm-lock.yaml", ["**/pnpm-lock.yaml"]))
        self.assertTrue(glob_match("a/b/index.ts", ["**/index.ts"]))
        self.assertFalse(glob_match("a/b/index.tsx.bak", ["**/index.ts"]))
        self.assertTrue(glob_match("docs/a/b.md", ["docs/**"]))
        self.assertFalse(glob_match("src/a.ts", ["*.ts"]))


class TestValidator(unittest.TestCase):
    def test_subset(self):
        s = {"type": "object", "required": ["a"], "additionalProperties": False,
             "properties": {"a": {"type": "array", "minItems": 1, "items": {"enum": [1, 2]}}}}
        self.assertEqual(validate({"a": [1]}, s), [])
        self.assertTrue(validate({"a": []}, s))
        self.assertTrue(validate({"a": [3]}, s))
        self.assertTrue(validate({"a": [1], "b": 1}, s))
        self.assertTrue(validate({}, s))

    def test_any_of(self):
        s = {"anyOf": [{"required": ["x"]}, {"required": ["y"]}]}
        self.assertEqual(validate({"y": 1}, s), [])
        self.assertTrue(validate({}, s))

    def test_bool_is_not_number(self):
        self.assertTrue(validate(True, {"type": "integer"}))

    def test_unknown_keyword_raises(self):
        with self.assertRaises(ValueError):
            validate({}, {"$ref": "#/x"})

    def test_repo_schemas_use_supported_subset(self):
        from plannerlib.common import load_schema
        for name in ("input-brief.schema.json", "plan-draft.schema.json", "plan.schema.json"):
            validate({}, load_schema(name))  # raises on unsupported keywords


if __name__ == "__main__":
    unittest.main()
