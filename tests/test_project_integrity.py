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

    def _get_function(self, module_path, func_name):
        with open(module_path, "r", encoding="utf-8") as source:
            tree = ast.parse(source.read(), filename=module_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return node
        self.fail(f"Función {func_name} no encontrada en {module_path}")

    def _find_calls(self, func_node, attr_name):
        """Devuelve las llamadas a <objeto>.<attr_name> dentro de la función."""
        calls = []
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == attr_name:
                    calls.append(node)
        return calls

    def test_ask_master_password_has_enter_bind_connected_to_on_submit(self):
        """Regresión V8: el diálogo de Configuración perdió el bind de Enter.

        ask_master_password() debe conectar la tecla <Return> del entry a on_submit
        para que la contraseña pueda enviarse sin botón.
        """
        auth_path = os.path.join(PROJECT_ROOT, "src", "core", "auth.py")
        func = self._get_function(auth_path, "ask_master_password")

        bind_calls = [c for c in self._find_calls(func, "bind") if c.args and isinstance(c.args[0], ast.Constant)]
        return_binds = [c for c in bind_calls if c.args[0].value == "<Return>"]
        self.assertTrue(return_binds, "ask_master_password() no tiene entry.bind('<Return>', on_submit)")

        bind = return_binds[0]
        # entry.bind(...) -> func.value debe ser el Name 'entry'
        self.assertIsInstance(bind.func.value, ast.Name)
        self.assertEqual(bind.func.value.id, "entry")
        # El handler debe ser on_submit (Name, no lambda ni otra función)
        self.assertTrue(bind.args and len(bind.args) > 1)
        self.assertIsInstance(bind.args[1], ast.Name)
        self.assertEqual(bind.args[1].id, "on_submit")

    def test_ask_master_password_has_accept_button_connected_to_on_submit(self):
        """Regresión V8: el diálogo de Configuración perdió el botón Aceptar.

        ask_master_password() debe crear un CTkButton cuyo command sea on_submit.
        """
        auth_path = os.path.join(PROJECT_ROOT, "src", "core", "auth.py")
        func = self._get_function(auth_path, "ask_master_password")

        button_calls = self._find_calls(func, "CTkButton")
        self.assertTrue(button_calls, "ask_master_password() no crea ningún CTkButton")

        submit_buttons = [
            c for c in button_calls
            if any(kw.arg == "command" and isinstance(kw.value, ast.Name) and kw.value.id == "on_submit" for kw in c.keywords)
        ]
        self.assertTrue(
            submit_buttons,
            "ask_master_password() no tiene ningún CTkButton con command=on_submit",
        )
