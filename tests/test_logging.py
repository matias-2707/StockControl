"""
Tests del sistema de logging persistente (V8.0).

Cubren:
- creación del directorio/log;
- escritura de mensajes;
- persistencia entre dos inicializaciones;
- rotación (1 MB / backups);
- registro desde múltiples threads;
- manejo de log inexistente/vacío (read_log_lines).

Los tests usan directorios temporales: nunca tocan el log real de la app.
"""

import os
import sys
import unittest
import tempfile
import shutil
import threading

# Asegurar root en sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.logger import (
    build_logger,
    read_log_lines,
    default_log_dir,
    DEFAULT_MAX_BYTES,
    DEFAULT_BACKUP_COUNT,
)


class TestLoggerSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scc_log_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_dir_and_file(self):
        log_dir = os.path.join(self.tmp, "logs")
        logger, log_file = build_logger(log_dir=log_dir, name="test_creates")
        logger.info("hola log")
        self.assertTrue(os.path.isdir(log_dir))
        self.assertTrue(os.path.isfile(log_file))

    def test_writes_message(self):
        logger, log_file = build_logger(log_dir=self.tmp, name="test_writes")
        logger.info("mensaje de prueba 12345")
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("mensaje de prueba 12345", content)
        self.assertIn("INFO", content)

    def test_persists_across_initializations(self):
        # Primera "ejecución"
        logger, log_file = build_logger(log_dir=self.tmp, name="test_persist")
        logger.info("primera sesion")
        # Segunda "ejecución" (re-init del logger sobre el mismo archivo)
        logger2, log_file2 = build_logger(log_dir=self.tmp, name="test_persist")
        logger2.info("segunda sesion")
        self.assertEqual(log_file, log_file2)
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("primera sesion", content)
        self.assertIn("segunda sesion", content)

    def test_rotation(self):
        # Forzamos rotación con 1 KB por archivo y 2 backups
        logger, log_file = build_logger(
            log_dir=self.tmp, name="test_rotation",
            max_bytes=1024, backup_count=2,
        )
        for i in range(500):
            logger.info("linea de relleno numero %d para forzar rotacion", i)
        backups = [f for f in os.listdir(self.tmp) if f.startswith("app.log")]
        self.assertGreater(len(backups), 1)
        self.assertTrue(any(f == "app.log.1" for f in backups))

    def test_thread_safety(self):
        logger, log_file = build_logger(log_dir=self.tmp, name="test_threads")
        n_threads = 8
        msgs_per_thread = 50
        errors = []

        def worker(tid):
            try:
                for i in range(msgs_per_thread):
                    logger.info("thread %d mensaje %d", tid, i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), n_threads * msgs_per_thread)

    def test_read_log_inexistent(self):
        result = read_log_lines(os.path.join(self.tmp, "no_existe.log"))
        self.assertEqual(len(result), 1)
        self.assertIn("No existe", result[0])

    def test_read_log_empty(self):
        empty_file = os.path.join(self.tmp, "vacio.log")
        open(empty_file, "w", encoding="utf-8").close()
        result = read_log_lines(empty_file)
        self.assertEqual(len(result), 1)
        self.assertIn("vacío", result[0])

    def test_read_log_large_file_only_tail(self):
        big_file = os.path.join(self.tmp, "grande.log")
        with open(big_file, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(f"linea {i}\n")
        result = read_log_lines(big_file, max_lines=10)
        self.assertLessEqual(len(result), 11)  # 10 líneas + aviso
        self.assertIn("Mostrando las últimas 10", result[0])

    def test_read_log_read_error(self):
        # Permisos 000 -> error de lectura (en Windows puede no aplicar;
        # en ese caso el archivo simplemente se lee; testeamos robustez).
        locked = os.path.join(self.tmp, "bloqueado.log")
        with open(locked, "w", encoding="utf-8") as f:
            f.write("x\n")
        os.chmod(locked, 0)
        result = read_log_lines(locked)
        # No debe crashear; devuelve o el contenido o un mensaje de error
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 1)

    def test_default_log_dir(self):
        d = default_log_dir()
        self.assertTrue(d.endswith("StockCellularCenter") or d.endswith("logs"))
        self.assertTrue(os.path.isabs(d))


if __name__ == "__main__":
    unittest.main()
