"""Checks that do not start the GUI or write into the application project."""

import ast
import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestProjectIntegrity(unittest.TestCase):
    def test_all_python_sources_parse(self):
        for root, dirs, files in os.walk(PROJECT_ROOT):
            dirs[:] = [name for name in dirs if name not in {"__pycache__", ".keys"}]
            for filename in files:
                if filename.endswith(".py"):
                    path = os.path.join(root, filename)
                    with open(path, "r", encoding="utf-8") as source:
                        ast.parse(source.read(), filename=path)

    def test_distribution_tree_has_no_private_key(self):
        forbidden_names = {"license_ed25519_private.pem"}
        findings = []
        for root, dirs, files in os.walk(PROJECT_ROOT):
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            for filename in files:
                if filename in forbidden_names or filename.endswith(".key"):
                    findings.append(os.path.join(root, filename))
        self.assertEqual(findings, [])

    def test_runtime_license_is_not_in_distribution_tree(self):
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "license.dat")))
