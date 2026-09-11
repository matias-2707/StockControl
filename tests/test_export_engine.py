"""
Tests del motor de exportación — Stock Cellular Center V8.1 (revisión integridad)

Revisión 2026-09-01 (auditoría de regresión V7.1 -> V8):

¿Por qué los tests anteriores daban 100% mientras la prueba manual fallaba?
- El receptor de prueba era un RecordingBackend que recibía STRINGS COMPLETOS
  (append a una lista). Ese receptor NO PUEDE perder caracteres, filtrar 'v',
  ni recibir Enter antes del paste: modela un canal sin pérdidas, idealizado.
- Los backends reales se probaban contra FakeController que registraba las
  LLAMADAS a la API (type/press/release), no el flujo de eventos que un
  receptor ocupado realmente procesa (con latencia, muestreo de modificadores
  y buffer limitado).
- Conclusión: verificaban "el motor llama a la API en orden", NO "el receptor
  recibe exactamente input==output".

Qué hace esta versión:
- Un receptor REALISTA (SlowReceiver) con tiempo virtual, determinista y
  PARAMETRIZABLE por perfil (ReceiverProfile). Cada test declara qué endpoint
  modela (ya no hay un único receptor compartido):
  * PROFILE_CURRENT: endpoint actual (ventana ocupada 40ms; paste aplicado
    hasta 25ms bajo carga) -> valida el pacing NUEVO;
  * PROFILE_STRICT: endpoint lento/contendido de la auditoría original
    (ventana ocupada 120ms; paste hasta 60ms bajo carga) -> delata el pacing
    VIEJO;
  * latencia por evento (1ms en reposo, 12ms ocupado), ventana ocupada tras
    cada Enter y buffer limitado (desbordamiento de carácter).
  * muestreo ASÍNCRONO del modificador Ctrl al procesar el keydown de 'v'
    (semántica GetAsyncKeyState): si el keyup de Ctrl ya fue inyectado, la
    tecla 'v' se emite literal -> artefacto 'v' real;
  * aplicación del paste con latencia (8ms en reposo; 25ms/60ms ocupado según
    perfil): si Enter se procesa antes, Enter confirma un campo vacío.
- Los backends REALES (TypingBackend/ClipboardBackend) se ejecutan contra un
  TimedController + reloj virtual: los eventos llevan tiempo virtual y el
  SlowReceiver los consume tal cual los produce el motor.
- Verificación de integridad estricta para typing/clipboard en 1x/3x/5x,
  con 100 códigos y varias iteraciones (corrupción intermitente).
- Un test que reproduce el pacing ANTERIOR (pisos < 1x probado, Ctrl+V sin
  esperas) y demuestra que el receptor modelo lo detecta como corrupto:
  protege contra volver a las constantes defectuosas.

Los benchmarks de gran volumen (receptor Tk real con pynput en la sesión
interactiva) viven FUERA del repo, en C:\\Temp\\audit_export\\.
"""

import os
import sys
import threading
import time
import unittest
from dataclasses import dataclass

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.automation import (
    AutomationManager,
    ClipboardBackend,
    DEFAULT_TYPING_SPEED,
    ExportEngine,
    ExportState,
    TYPING_PROFILES,
    TYPING_SPEED_MAX,
    TypingBackend,
    build_backend,
    build_export_list,
    clamp_typing_speed,
    compute_pacing,
    selector_semantics,
    typing_profile,
)
from pynput import keyboard


# ---------------------------------------------------------------------------
# Datos sintéticos realistas
# ---------------------------------------------------------------------------

def make_codes(n):
    """SKUs similares a los reales: prefijos AG/AM/AO, dígitos, guiones,
    y algunos con unicode (Ñ) y puntos para detectar corrupción de caracteres."""
    prefixes = ["AG", "AM", "AO"]
    codes = []
    for i in range(n):
        p = prefixes[i % 3]
        num = f"{i + 1:06d}"
        if i % 23 == 0:
            code = f"{p}-{num}/Ñ-{i % 7}"
        elif i % 17 == 0:
            code = f"{p}.{num}.{i % 9}"
        else:
            code = f"{p}-{num}"
        codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# Tiempo virtual + controller con timestamps
# ---------------------------------------------------------------------------

class VirtualClock:
    """Reemplaza time.sleep: acumula tiempo virtual, no bloquea."""

    def __init__(self):
        self.now = 0.0

    def __call__(self, seconds):
        self.now += max(0.0, seconds)


def _key_name(key):
    if key is keyboard.Key.ctrl:
        return "ctrl"
    if key is keyboard.Key.enter:
        return "enter"
    return key


class TimedController:
    """Controller que registra cada evento con el tiempo virtual actual."""

    def __init__(self, clock):
        self.clock = clock
        self.events = []  # (tiempo_virtual, kind, key_normalizada)

    def _rec(self, kind, key):
        self.events.append((self.clock.now, kind, _key_name(key)))

    def type(self, char):
        self._rec("type", char)

    def press(self, key):
        self._rec("press", key)

    def release(self, key):
        self._rec("release", key)

    def pressed(self, key):
        return _PressedCtx(self, key)


class _PressedCtx:
    def __init__(self, ctrl, key):
        self.ctrl = ctrl
        self.key = key

    def __enter__(self):
        self.ctrl.press(self.key)
        return self

    def __exit__(self, *a):
        self.ctrl.release(self.key)


class TimedClipboard:
    """Portapapeles que registra el momento virtual de cada copia."""

    def __init__(self, clock):
        self.clock = clock
        self.value = ""
        self.history = []  # (tiempo_virtual, texto)
        self.copy_calls = 0
        self._last_copy_time = 0.0
        self.fail_copies = 0
        self._mutate_after_copy = False  # simula reemplazo externo del clipboard

    def copy(self, text):
        self.copy_calls += 1
        if self.fail_copies > 0:
            self.fail_copies -= 1
            raise RuntimeError("clipboard ocupado (simulado)")
        self.value = text
        self._last_copy_time = self.clock.now
        self.history.append((self.clock.now, text))

    def paste(self):
        if self._mutate_after_copy and self.clock.now > self._last_copy_time + 0.050:
            # otra app reemplazó el clipboard después de la copia (la lectura
            # de verificación de copia ocurre ~0ms después; la verificación
            # post-entrega del motor, cientos de ms después)
            return self.value + "-MUTADO"
        return self.value


# ---------------------------------------------------------------------------
# Receptor realista (modelo del navegador/ERP ocupado) — tiempo virtual
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReceiverProfile:
    """Modelo de latencia de un endpoint receptor.

    NO es una verdad universal: cada perfil describe un escenario concreto.
    Un mismo pacing puede ser seguro contra un perfil y corrupto contra otro;
    por eso cada test declara explícitamente qué receptor modela.
    """
    base_latency: float
    busy_latency: float
    busy_window: float
    paste_latency: float
    busy_paste_latency: float
    cap_wait: float


# Perfil REPRESENTATIVO del endpoint actual (pacing rápido de Clipboard).
# Modela un ERP/navegador que aplica el paste con rapidez y cuya ventana
# ocupada es corta. Lo usan los tests de INTEGRIDAD del pacing nuevo.
PROFILE_CURRENT = ReceiverProfile(
    base_latency=0.001,          # 1ms por evento en reposo
    busy_latency=0.012,          # 12ms por evento durante ventana ocupada
    busy_window=0.040,           # 40ms de ventana ocupada tras cada Enter
    paste_latency=0.008,         # 8ms en aplicar el paste en reposo
    busy_paste_latency=0.025,    # 25ms en aplicar el paste bajo carga
    cap_wait=0.100,              # >100ms de espera => se descarta el carácter
)

# Perfil ESTRICTO/LENTO: el modelo conservador de la auditoría original
# (ventana ocupada de 120ms; el paste puede tardar hasta 60ms bajo carga).
# Es el escenario para el que el pacing ANTIGUO era inseguro: lo usan los
# tests de RECHAZO del pacing legacy como guard de regresión.
PROFILE_STRICT = ReceiverProfile(
    base_latency=0.001,
    busy_latency=0.012,
    busy_window=0.120,
    paste_latency=0.008,
    busy_paste_latency=0.060,
    cap_wait=0.100,
)


class SlowReceiver:
    """Receptor virtual con latencia de procesamiento, PARAMETRIZABLE.

    El comportamiento depende del ReceiverProfile recibido (ya no de constantes
    globales compartidas), de modo que cada test declara qué endpoint modela:

    - PROFILE_CURRENT: endpoint actual  -> TestRealTransportIntegrity.
    - PROFILE_STRICT:  endpoint lento   -> TestLegacyPacingIsRejected.

    Semántica del modelo:
    - latencia por evento (reposo/ocupado) y ventana ocupada tras cada Enter;
    - el estado de Ctrl se muestrea de forma ASÍNCRONA (GetAsyncKeyState): al
      procesar el keydown de 'v' vale el estado de los eventos de Ctrl ya
      INYECTADOS hasta ese instante, no el de los procesados;
    - el paste se aplica con latencia; si Enter se procesa antes, la línea sale
      vacía;
    - buffer limitado: un evento que espera más de cap_wait se descarta.
    """

    def __init__(self, profile=PROFILE_CURRENT):
        self.profile = profile
        self.BASE_LATENCY = profile.base_latency
        self.BUSY_LATENCY = profile.busy_latency
        self.BUSY_WINDOW = profile.busy_window
        self.PASTE_LATENCY = profile.paste_latency
        self.BUSY_PASTE_LATENCY = profile.busy_paste_latency
        self.CAP_WAIT = profile.cap_wait
        self.free_at = 0.0
        self.busy_until = 0.0
        self.field = ""
        self.lines = []
        self.pending_pastes = []     # (apply_at, texto)
        self.ctrl_events = []        # (tiempo_inyección, 'press'|'release')
        self.dropped = 0
        self.v_leaks = 0
        self.clipboard = ""
        self._clipboard_history = []

    def run(self, events, clipboard_history):
        self._clipboard_history = list(clipboard_history)
        # log de inyección completo: el estado asíncrono avanza con la
        # inyección, aunque el receptor procese con retraso
        self.ctrl_events = [(t, k) for t, k, key in events if key == "ctrl"]
        for t, kind, key in events:
            while self._clipboard_history and self._clipboard_history[0][0] <= t:
                self.clipboard = self._clipboard_history.pop(0)[1]
            self._process(t, kind, key)

    def _process(self, t_inject, kind, key):
        start = max(self.free_at, t_inject)
        busy = start < self.busy_until
        latency = self.BUSY_LATENCY if busy else self.BASE_LATENCY
        if start - t_inject > self.CAP_WAIT:
            if kind == "type" or (kind == "press" and key not in ("ctrl", "enter")):
                self.dropped += 1
                return
        end = start + latency
        self.free_at = end

        if kind == "type":
            self.field += key
        elif kind == "press" and key == "v":
            last_ctrl = [e for e in self.ctrl_events if e[0] <= start]
            ctrl_down = bool(last_ctrl) and last_ctrl[-1][1] == "press"
            if not ctrl_down:
                self.field += "v"
                self.v_leaks += 1
            else:
                apply_at = end + (self.BUSY_PASTE_LATENCY if busy else self.PASTE_LATENCY)
                self.pending_pastes.append((apply_at, self.clipboard))
        elif kind == "press" and key == "enter":
            for at, txt in list(self.pending_pastes):
                if at <= start:
                    self.field += txt
                    self.pending_pastes.remove((at, txt))
            self.lines.append(self.field)
            self.field = ""
            self.busy_until = start + self.BUSY_WINDOW


# ---------------------------------------------------------------------------
# Receptor idealizado (para tests de ESTADOS del motor, no de transporte)
# ---------------------------------------------------------------------------

class RecordingBackend:
    """Receptor controlado: registra cada código y cada Enter que recibe.

    Solo se usa para verificar la MÁQUINA DE ESTADOS del motor (pausa,
    cancelación, errores, reporte) — NO para validar integridad de
    transporte (para eso está SlowReceiver).
    """

    name = "recording"
    inter_code_gap = 0.0
    applied_multiplier = 1.0

    def __init__(self, fail_at=None):
        self.received = []
        self.enters = 0
        self.log = []
        self.fail_at = fail_at
        self.block_events = threading.Event()
        self.release_events = threading.Event()
        self.in_code = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0

    def send_code(self, code):
        with self._lock:
            self.calls += 1
        self.in_code.set()
        if self.block_events.is_set():
            self.release_events.wait(5.0)
        with self._lock:
            if self.fail_at is not None and len(self.received) >= self.fail_at:
                raise RuntimeError("boom simulado del backend")
            self.received.append(code)
            self.log.append(("code", code))
        self._send_enter()
        self.in_code.clear()

    def _send_enter(self):
        with self._lock:
            self.enters += 1
            self.log.append(("enter", None))

    def verify(self, code):
        return True

    def snapshot(self):
        with self._lock:
            return list(self.received), list(self.log), self.enters


# ---------------------------------------------------------------------------
# Helpers para correr los backends reales contra el receptor modelo
# ---------------------------------------------------------------------------

def run_backend_against_receiver(backend_cls, codes, multiplier, mode,
                                 backend_kwargs=None):
    """Ejecuta un backend real (tiempo virtual) y consume sus eventos con el
    SlowReceiver. Devuelve (lines_del_receptor, reporte, evento_log)."""
    clock = VirtualClock()
    ctrl = TimedController(clock)
    clip = TimedClipboard(clock)
    kwargs = dict(backend_kwargs or {})
    if mode == "clipboard":
        kwargs.setdefault("clipboard", clip)
    kwargs.setdefault("controller", ctrl)
    kwargs.setdefault("sleep_fn", clock)
    kwargs.setdefault("multiplier", multiplier)
    backend = backend_cls(**kwargs)
    engine = ExportEngine(sleep_fn=clock)
    report = engine.run(codes, backend)
    rx = SlowReceiver()
    rx.run(ctrl.events, clip.history)
    return rx, report


def legacy_typing_events(codes, char_delay=0.008, enter_settle=0.010, gap=0.020):
    """Secuencia de eventos que producía el pacing V8 ANTERIOR (8ms/car)."""
    t = 0.0
    ev = []
    for code in codes:
        for ch in code:
            ev.append((t, "type", ch))
            t += char_delay
        ev.append((t, "press", "enter"))
        t += 0.001
        ev.append((t, "release", "enter"))
        t += enter_settle
        t += gap
    return ev, []


def legacy_clip_events(codes, copy_settle=0.0167, paste_enter=0.030, gap=0.030):
    """Secuencia del clipboard V8 ANTERIOR: Ctrl+V sin esperas y pisos < 1x
    (copy_settle 16.7ms, paste->Enter 30ms, gap 30ms — valores a 3x)."""
    t = 0.0
    ev = []
    clips = []
    for code in codes:
        clips.append((t, code))
        t += copy_settle
        ev.append((t, "press", "ctrl"))
        t += 0.001
        ev.append((t, "press", "v"))
        t += 0.001
        ev.append((t, "release", "v"))
        t += 0.001
        ev.append((t, "release", "ctrl"))
        t += paste_enter
        ev.append((t, "press", "enter"))
        t += 0.001
        ev.append((t, "release", "enter"))
        t += 0.010
        t += gap
    return ev, clips


# ---------------------------------------------------------------------------
# 1. Construcción de la lista a exportar
# ---------------------------------------------------------------------------

class TestExportListBuilding(unittest.TestCase):
    def test_order_is_scan_order(self):
        scanned = {
            "AG-000001": [4, 7],
            "AM-000002": [1],
            "AO-000003": [2, 9],
        }
        codes = build_export_list(scanned, "", {"AG-000001": "AG", "AM-000002": "AM", "AO-000003": "AO"}, frozenset())
        self.assertEqual(codes, ["AM-000002", "AO-000003", "AG-000001", "AG-000001", "AO-000003"])

    def test_family_filter_prefix(self):
        scanned = {"AG-100": [1], "AGX-200": [2], "AM-300": [3], "AO-400": [4]}
        fam = {"AG-100": "AG", "AGX-200": "AGX", "AM-300": "AM", "AO-400": "AO"}
        codes = build_export_list(scanned, "AG", fam, frozenset())
        self.assertEqual(codes, ["AG-100", "AGX-200"])

    def test_excluded_skus_skipped(self):
        scanned = {"AG-100": [1], "AM-200": [2]}
        fam = {"AG-100": "AG", "AM-200": "AM"}
        codes = build_export_list(scanned, "AM", fam, frozenset({"AM-200"}))
        self.assertEqual(codes, [])

    def test_unknown_family_excluded(self):
        scanned = {"ZZ-100": [1]}
        fam = {}
        codes = build_export_list(scanned, "AG", fam, frozenset())
        self.assertEqual(codes, [])


# ---------------------------------------------------------------------------
# 2. INTEGRIDAD REAL: backends reales + receptor modelo, con repetición
# ---------------------------------------------------------------------------

class TestRealTransportIntegrity(unittest.TestCase):
    """input == output contra el receptor con latencia real.

    - misma cantidad; mismo orden; cero duplicados; cero pérdidas; cero
      caracteres adicionales; cero líneas vacías inesperadas.
    - 100 códigos x 3 iteraciones por caso (corrupción intermitente).
    """

    N = 100
    ITERATIONS = 3

    def _assert_exact(self, rx, codes):
        self.assertEqual(
            rx.lines, codes,
            f"corrupción: v_leaks={rx.v_leaks}, dropped={rx.dropped}, "
            f"líneas={len(rx.lines)}/{len(codes)}"
        )
        self.assertEqual(rx.v_leaks, 0, "artefactos 'v' (Ctrl liberado antes del keydown)")
        self.assertEqual(rx.dropped, 0, "caracteres descartados por desbordamiento")
        self.assertNotIn("", rx.lines, "líneas vacías (Enter antes del paste)")

    def test_typing_1x(self):
        for _ in range(self.ITERATIONS):
            rx, report = run_backend_against_receiver(TypingBackend, make_codes(self.N), 1.0, "typing")
            self.assertEqual(report.state, ExportState.COMPLETED)
            self._assert_exact(rx, make_codes(self.N))

    def test_typing_3x(self):
        for _ in range(self.ITERATIONS):
            rx, _ = run_backend_against_receiver(TypingBackend, make_codes(self.N), 3.0, "typing")
            self._assert_exact(rx, make_codes(self.N))

    def test_typing_5x(self):
        for _ in range(self.ITERATIONS):
            rx, _ = run_backend_against_receiver(TypingBackend, make_codes(self.N), 5.0, "typing")
            self._assert_exact(rx, make_codes(self.N))

    def test_clipboard_1x(self):
        for _ in range(self.ITERATIONS):
            rx, _ = run_backend_against_receiver(ClipboardBackend, make_codes(self.N), 1.0, "clipboard")
            self._assert_exact(rx, make_codes(self.N))

    def test_clipboard_3x(self):
        for _ in range(self.ITERATIONS):
            rx, _ = run_backend_against_receiver(ClipboardBackend, make_codes(self.N), 3.0, "clipboard")
            self._assert_exact(rx, make_codes(self.N))

    def test_clipboard_5x(self):
        # 5x solicitado -> el motor lo capea al máximo seguro; igual integridad
        for _ in range(self.ITERATIONS):
            rx, _ = run_backend_against_receiver(ClipboardBackend, make_codes(self.N), 5.0, "clipboard")
            self._assert_exact(rx, make_codes(self.N))

    def test_no_duplicates_no_losses_no_concat(self):
        codes = make_codes(300)
        rx, _ = run_backend_against_receiver(ClipboardBackend, codes, 3.0, "clipboard")
        self.assertEqual(len(rx.lines), len(set(rx.lines)), "sin duplicados")
        self.assertEqual(len(rx.lines), len(codes), "sin pérdidas")
        self.assertEqual(rx.lines, codes, "mismo orden y contenido")


# ---------------------------------------------------------------------------
# 3. El pacing ANTERIOR era corrupto: el modelo lo detecta (guard de regresión)
# ---------------------------------------------------------------------------

class TestLegacyPacingIsRejected(unittest.TestCase):
    """Protege contra volver a las constantes defectuosas de V8 anterior.

    Usa el receptor ESTRICTO (PROFILE_STRICT), que modela el endpoint lento/
    contendido para el que el pacing viejo era inseguro. El pacing nuevo se
    valida por separado contra PROFILE_CURRENT (representativo del endpoint
    actual). Deliberadamente NO comparten receptor: un mismo receptor no puede
    ser a la vez lo bastante tolerante para no romper el pacing nuevo y lo
    bastante estricto para delatar el legacy.

    Bajo PROFILE_STRICT el pacing viejo (8ms/car en typing; Ctrl+V sin esperas
    + pisos por debajo del 1x seguro en clipboard) corrompe tal como en la
    evidencia real: 'v' literales y caracteres perdidos.

    NOTA: esto NO afirma que esos timings sean inseguros contra CUALQUIER
    receptor; afirma que lo son contra un endpoint estricto, que es el
    escenario que este guard protege.
    """

    def test_receivers_are_separated_by_profile(self):
        # Deja explícito el diseño: el pacing legacy NO es "universalmente
        # inseguro". Contra el receptor representativo actual puede pasar;
        # contra el estricto corrompe. Por eso cada clase usa un perfil propio.
        codes = make_codes(100)
        ev, clips = legacy_clip_events(codes)
        rx_current = SlowReceiver(PROFILE_CURRENT)
        rx_current.run(ev, clips)
        rx_strict = SlowReceiver(PROFILE_STRICT)
        rx_strict.run(ev, clips)
        self.assertEqual(rx_current.lines, codes,
                         "contra el receptor representativo el legacy no corrompe")
        self.assertNotEqual(rx_strict.lines, codes,
                            "contra el receptor estricto el legacy SÍ corrompe")

    def test_legacy_typing_1x_corrupts(self):
        codes = make_codes(100)
        ev, clips = legacy_typing_events(codes)
        rx = SlowReceiver(PROFILE_STRICT)
        rx.run(ev, clips)
        self.assertNotEqual(rx.lines, codes, "el pacing viejo NO debe pasar el receptor estricto")
        self.assertGreater(rx.dropped, 0, "el receptor estricto debe detectar pérdida de caracteres")

    def test_legacy_clipboard_3x_corrupts(self):
        codes = make_codes(100)
        ev, clips = legacy_clip_events(codes)
        rx = SlowReceiver(PROFILE_STRICT)
        rx.run(ev, clips)
        self.assertNotEqual(rx.lines, codes, "el pacing viejo NO debe pasar el receptor estricto")
        self.assertGreater(rx.v_leaks + rx.lines.count(""), 0,
                           "el receptor estricto debe detectar 'v' literales o líneas vacías")

    def test_legacy_clipboard_1x_also_corrupts_at_volume(self):
        # El 1x viejo (50ms) pasaba en 98 códigos reales pero corrompía a
        # volumen (benchmark real: 1700 -> 11 missing). El receptor estricto
        # lo refleja.
        codes = make_codes(400)
        ev, clips = legacy_clip_events(codes, copy_settle=0.050, paste_enter=0.050, gap=0.020)
        rx = SlowReceiver(PROFILE_STRICT)
        rx.run(ev, clips)
        self.assertNotEqual(rx.lines, codes)


# ---------------------------------------------------------------------------
# 4. Mecánica de los backends: secuencia y tiempos (Ctrl+V con esperas)
# ---------------------------------------------------------------------------

class TestClipboardBackendMechanics(unittest.TestCase):
    def _run(self, multiplier):
        clock = VirtualClock()
        ctrl = TimedController(clock)
        clip = TimedClipboard(clock)
        backend = ClipboardBackend(controller=ctrl, clipboard=clip, sleep_fn=clock, multiplier=multiplier)
        backend.send_code("AG-000001")
        return ctrl.events, clip

    def test_ctrl_hold_before_v(self):
        events, _ = self._run(1.0)
        t_ctrl_down = next(t for t, k, key in events if k == "press" and key == "ctrl")
        t_v_down = next(t for t, k, key in events if k == "press" and key == "v")
        self.assertGreaterEqual(t_v_down - t_ctrl_down, ClipboardBackend.CTRL_HOLD - 1e-9,
                                "Ctrl debe mantenerse presionado antes del keydown de 'v'")

    def test_ctrl_released_after_v(self):
        events, _ = self._run(1.0)
        t_v_down = next(t for t, k, key in events if k == "press" and key == "v")
        t_ctrl_up = next(t for t, k, key in events if k == "release" and key == "ctrl")
        self.assertGreaterEqual(t_ctrl_up - t_v_down, 2 * ClipboardBackend.KEY_HOLD - 1e-9,
                                "Ctrl debe mantenerse presionado después del keydown de 'v'")

    def test_enter_after_paste_enter_gap(self):
        events, _ = self._run(1.0)
        t_v_down = next(t for t, k, key in events if k == "press" and key == "v")
        t_enter = next(t for t, k, key in events if k == "press" and key == "enter")
        self.assertGreaterEqual(t_enter - t_v_down, ClipboardBackend.PASTE_ENTER_GAP_1X - 1e-9,
                                "Enter nunca antes del piso paste->Enter")

    def test_paste_enter_gap_never_below_1x_at_3x(self):
        p = compute_pacing(3.0, "clipboard")
        self.assertEqual(p["paste_enter_gap"], ClipboardBackend.PASTE_ENTER_GAP_1X,
                         "el gap paste->Enter NO se comprime con el multiplicador")

    def test_inter_code_gap_never_below_1x_at_3x(self):
        p = compute_pacing(3.0, "clipboard")
        self.assertEqual(p["inter_code_gap"], ClipboardBackend.GAP_1X,
                         "el gap entre códigos NO se comprime con el multiplicador")

    def test_copy_verify_paste_enter_order(self):
        events, clip = self._run(1.0)
        self.assertEqual(clip.value, "AG-000001")
        self.assertEqual(clip.copy_calls, 1)
        kinds = [k for _, k, _ in events]
        keys = [key for _, _, key in events]
        # secuencia completa: press ctrl, press v, release v, release ctrl,
        # press enter, release enter (con esperas entre medio)
        self.assertEqual(kinds[0], "press")
        self.assertEqual(keys[0], "ctrl")
        self.assertIn("v", keys)
        self.assertEqual(kinds[-2:], ["press", "release"])
        self.assertEqual(keys[-2:], ["enter", "enter"])

    def test_copy_retry_on_contention(self):
        clock = VirtualClock()
        ctrl = TimedController(clock)
        clip = TimedClipboard(clock)
        clip.fail_copies = 2
        backend = ClipboardBackend(controller=ctrl, clipboard=clip, sleep_fn=clock, multiplier=1.0)
        backend.send_code("AG-000003")
        self.assertEqual(clip.copy_calls, 3)

    def test_raises_after_max_retries(self):
        clock = VirtualClock()
        ctrl = TimedController(clock)
        clip = TimedClipboard(clock)
        clip.fail_copies = 99
        backend = ClipboardBackend(controller=ctrl, clipboard=clip, sleep_fn=clock, multiplier=1.0)
        with self.assertRaises(RuntimeError):
            backend.send_code("AG-000004")


class TestTypingBackendMechanics(unittest.TestCase):
    def test_chars_and_enter_per_code(self):
        clock = VirtualClock()
        ctrl = TimedController(clock)
        backend = TypingBackend(controller=ctrl, sleep_fn=clock, multiplier=1.0)
        backend.send_code("AG-000001")
        types = [ch for _, k, ch in ctrl.events if k == "type"]
        self.assertEqual("".join(types), "AG-000001")
        keys = [key for _, _, key in ctrl.events]
        self.assertEqual(keys[-2:], ["enter", "enter"])

    def test_unicode_characters_typed(self):
        clock = VirtualClock()
        ctrl = TimedController(clock)
        backend = TypingBackend(controller=ctrl, sleep_fn=clock, multiplier=1.0)
        backend.send_code("AM-000001/Ñ-3")
        types = [ch for _, k, ch in ctrl.events if k == "type"]
        self.assertEqual("".join(types), "AM-000001/Ñ-3")

    def test_char_delay_floor(self):
        for mult in (3.0, 5.0):
            p = compute_pacing(mult, "typing")
            self.assertGreaterEqual(p["char_delay"], TypingBackend.CHAR_FLOOR)


# ---------------------------------------------------------------------------
# 5. Modelo de ritmo (determinista, sin wall-clock)
# ---------------------------------------------------------------------------

class TestPacing(unittest.TestCase):
    def test_floors_never_below_receiver_safe_values(self):
        for mult in (0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
            p = compute_pacing(mult, "clipboard")
            # gaps de receptor: nunca menores al valor 1x probado
            self.assertGreaterEqual(p["paste_enter_gap"], ClipboardBackend.PASTE_ENTER_GAP_1X)
            self.assertGreaterEqual(p["inter_code_gap"], ClipboardBackend.GAP_1X)
            self.assertGreaterEqual(p["ctrl_hold"], 0.0)
            p = compute_pacing(mult, "typing")
            self.assertGreaterEqual(p["char_delay"], TypingBackend.CHAR_FLOOR)
            self.assertGreaterEqual(p["inter_code_gap"], TypingBackend.GAP_1X)

    def test_applied_multiplier_never_exceeds_requested(self):
        for mult in (0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
            for backend in ("typing", "clipboard"):
                p = compute_pacing(mult, backend)
                self.assertLessEqual(p["applied_multiplier"], float(mult) + 1e-9)
                self.assertGreaterEqual(p["applied_multiplier"], 0.1)

    def test_effective_speed_is_honest(self):
        # 3x es más rápido que 1x (comprime solo tiempos del emisor), pero
        # 5x solicitado se capea: el efectivo de 5x == el de 3x (máx seguro)
        p1 = compute_pacing(1.0, "clipboard")
        p3 = compute_pacing(3.0, "clipboard")
        p5 = compute_pacing(5.0, "clipboard")
        self.assertGreater(p3["applied_multiplier"], p1["applied_multiplier"])
        self.assertEqual(p5["applied_multiplier"], p3["applied_multiplier"])
        self.assertLessEqual(p5["applied_multiplier"], 3.0)
        # y el efectivo real es honesto: con pisos de receptor, 3x ~ 1.1-1.3x
        self.assertLess(p3["applied_multiplier"], 2.0)

    def test_typing_5x_capped(self):
        p5 = compute_pacing(5.0, "typing")
        p3 = compute_pacing(3.0, "typing")
        self.assertEqual(p5["char_delay"], p3["char_delay"], "5x se capea al máximo seguro")


# ---------------------------------------------------------------------------
# 6. Estados y control de flujo (máquina de estados, receptor idealizado)
# ---------------------------------------------------------------------------

class TestExportStates(unittest.TestCase):
    def test_idle_to_running_to_completed(self):
        engine = ExportEngine()
        self.assertEqual(engine.state, ExportState.IDLE)
        report = engine.run(make_codes(10), RecordingBackend())
        self.assertEqual(report.state, ExportState.COMPLETED)

    def test_pause_resume(self):
        codes = make_codes(100)
        backend = RecordingBackend()
        backend.block_events.set()
        engine = ExportEngine()
        result = {}

        def target():
            result["report"] = engine.run(codes, backend)

        t = threading.Thread(target=target)
        t.start()
        backend.in_code.wait(5.0)
        engine.pause()
        self.assertEqual(engine.state, ExportState.PAUSED)
        backend.release_events.set()
        time.sleep(0.05)
        received, _, _ = backend.snapshot()
        self.assertEqual(len(received), 1)
        self.assertEqual(received, [codes[0]], "el código en curso se completa")
        engine.resume()
        self.assertEqual(engine.state, ExportState.RUNNING)
        backend.block_events.clear()
        t.join(10)
        received, _, _ = backend.snapshot()
        self.assertEqual(received, codes)
        self.assertEqual(result["report"].state, ExportState.COMPLETED)

    def test_cancel_finishes_current_code(self):
        codes = make_codes(50)
        backend = RecordingBackend()
        backend.block_events.set()
        engine = ExportEngine()
        result = {}

        def target():
            result["report"] = engine.run(codes, backend)

        t = threading.Thread(target=target)
        t.start()
        backend.in_code.wait(5.0)
        engine.cancel()
        backend.release_events.set()
        t.join(10)
        received, _, _ = backend.snapshot()
        self.assertEqual(received, [codes[0]], "el código en curso se completa, sin truncar")
        report = result["report"]
        self.assertEqual(report.state, ExportState.CANCELLED)
        self.assertEqual(report.sent, 1)

    def test_cancel_between_codes(self):
        codes = make_codes(100)
        backend = RecordingBackend()
        engine = ExportEngine()
        engine.cancel()
        report = engine.run(codes, backend)
        received, _, _ = backend.snapshot()
        self.assertEqual(report.state, ExportState.CANCELLED)
        self.assertEqual(report.sent, 0)
        self.assertEqual(received, [])

    def test_error_state_and_reuse(self):
        codes = make_codes(100)
        backend = RecordingBackend(fail_at=10)
        engine = ExportEngine()
        report = engine.run(codes, backend)
        self.assertEqual(report.state, ExportState.ERROR)
        self.assertEqual(report.sent, 10)
        self.assertIsNotNone(report.error)
        received, _, _ = backend.snapshot()
        self.assertEqual(received, codes[:10], "nada después del error")
        backend2 = RecordingBackend()
        report2 = engine.run(codes[:5], backend2)
        self.assertEqual(report2.state, ExportState.COMPLETED)
        self.assertEqual(report2.sent, 5)


# ---------------------------------------------------------------------------
# 7. Reporte honesto + verificación post-entrega
# ---------------------------------------------------------------------------

class TestExportReport(unittest.TestCase):
    def test_completed_counts(self):
        report = ExportEngine().run(make_codes(25), RecordingBackend())
        self.assertEqual(report.state, ExportState.COMPLETED)
        self.assertEqual(report.total, 25)
        self.assertEqual(report.sent, 25)
        self.assertIsNone(report.error)
        self.assertGreaterEqual(report.elapsed_s, 0)

    def test_cancelled_never_reports_completed(self):
        codes = make_codes(200)
        backend = RecordingBackend()
        backend.block_events.set()
        engine = ExportEngine()
        result = {}

        def target():
            result["report"] = engine.run(codes, backend)

        t = threading.Thread(target=target)
        t.start()
        backend.in_code.wait(5.0)
        engine.cancel()
        backend.release_events.set()
        t.join(10)
        report = result["report"]
        self.assertEqual(report.state, ExportState.CANCELLED)
        self.assertNotEqual(report.state, ExportState.COMPLETED)
        self.assertLess(report.sent, report.total)

    def test_error_reports_not_completed(self):
        report = ExportEngine().run(make_codes(50), RecordingBackend(fail_at=5))
        self.assertEqual(report.state, ExportState.ERROR)
        self.assertNotEqual(report.state, ExportState.COMPLETED)
        self.assertEqual(report.sent, 5)

    def test_verify_failures_counted(self):
        clock = VirtualClock()
        ctrl = TimedController(clock)
        clip = TimedClipboard(clock)
        clip._mutate_after_copy = True  # el clipboard cambia tras copiar
        backend = ClipboardBackend(controller=ctrl, clipboard=clip, sleep_fn=clock, multiplier=1.0)
        codes = make_codes(5)
        report = ExportEngine(sleep_fn=clock).run(codes, backend)
        self.assertEqual(report.state, ExportState.COMPLETED)
        self.assertEqual(report.verify_failures, 5, "cada código debe registrar la verificación fallida")


# ---------------------------------------------------------------------------
# 8. AutomationManager (facade) con backend inyectado
# ---------------------------------------------------------------------------

class _StubConfig:
    def __init__(self, speed=1.0, mode="typing"):
        self._d = {"speed_multiplier": speed, "paste_mode": mode}

    def get(self, key, default=None):
        return self._d.get(key, default)


class _StubInventory:
    config = _StubConfig()


class TestAutomationManagerIntegration(unittest.TestCase):
    def test_process_export_with_recording_backend(self):
        codes = make_codes(100)
        received_box = {}

        def factory(mode):
            backend = RecordingBackend()
            received_box["backend"] = backend
            return backend

        am = AutomationManager(_StubConfig(speed=1.0, mode="typing"), _StubInventory(), backend_factory=factory)
        report = am.process_export(codes, mode="typing")
        backend = received_box["backend"]
        received, _, _ = backend.snapshot()
        self.assertEqual(report.state, ExportState.COMPLETED)
        self.assertEqual(received, codes)

    def test_process_export_cancelled_report(self):
        codes = make_codes(100)
        backend = RecordingBackend()
        am = AutomationManager(_StubConfig(), _StubInventory(), backend_factory=lambda mode: backend)
        backend.block_events.set()

        def target():
            return am.process_export(codes, mode="typing")

        t = threading.Thread(target=lambda: setattr(am, "_test_report", target()))
        t.start()
        backend.in_code.wait(5.0)
        am.stop_automation()
        backend.release_events.set()
        t.join(10)
        report = getattr(am, "_test_report", None)
        self.assertIsNotNone(report)
        self.assertEqual(report.state, ExportState.CANCELLED)



# ---------------------------------------------------------------------------
# 9. Selector de exportacion: metodo / velocidad / Modo seguro
# ---------------------------------------------------------------------------

class TestSelectorSemantics(unittest.TestCase):
    def test_typing_enables_speed_selector(self):
        st = selector_semantics("typing", False)
        self.assertTrue(st["speed_enabled"])
        self.assertEqual(st["effective_method"], "typing")
        self.assertFalse(st["use_safe_mode"])

    def test_clipboard_disables_speed_selector(self):
        st = selector_semantics("clipboard", False)
        self.assertFalse(st["speed_enabled"])
        self.assertEqual(st["effective_method"], "clipboard")

    def test_safe_mode_disables_speed_and_forces_ultra(self):
        st = selector_semantics("typing", True)
        self.assertFalse(st["speed_enabled"])
        self.assertEqual(st["effective_method"], "clipboard")
        self.assertTrue(st["use_safe_mode"])

    def test_safe_mode_off_returns_to_typing(self):
        st = selector_semantics("typing", False)
        self.assertTrue(st["speed_enabled"])
        self.assertEqual(st["effective_method"], "typing")

    def test_clamp_typing_speed(self):
        self.assertEqual(clamp_typing_speed(0), 1)
        self.assertEqual(clamp_typing_speed(-5), 1)
        self.assertEqual(clamp_typing_speed(99), TYPING_SPEED_MAX)
        self.assertEqual(clamp_typing_speed("nope"), DEFAULT_TYPING_SPEED)

    def test_build_backend_typing_uses_profile(self):
        backend = build_backend("typing", 4, False)
        self.assertIsInstance(backend, TypingBackend)
        prof = typing_profile(4)
        self.assertEqual(backend.pacing["char_delay"], prof["char_delay"])
        self.assertEqual(backend.pacing["enter_settle"], prof["enter_settle"])
        self.assertEqual(backend.inter_code_gap, prof["gap"])

    def test_build_backend_safe_mode_uses_clipboard_ultra(self):
        backend = build_backend("typing", 4, True)
        self.assertIsInstance(backend, ClipboardBackend)
        self.assertEqual(backend.inter_code_gap, ClipboardBackend.ULTRA_SAFE["inter_code_gap"])
        self.assertEqual(backend.pacing["paste_enter_gap"], ClipboardBackend.ULTRA_SAFE["paste_enter_gap"])
        self.assertEqual(backend.pacing["copy_settle"], ClipboardBackend.ULTRA_SAFE["copy_settle"])

    def test_build_backend_clipboard_method_always_ultra(self):
        backend = build_backend("clipboard", 1, False)
        self.assertIsInstance(backend, ClipboardBackend)
        self.assertEqual(backend.pacing["enter_settle"], ClipboardBackend.ULTRA_SAFE["enter_settle"])

    def test_ultra_safe_is_conservative(self):
        up = ClipboardBackend.ULTRA_SAFE
        self.assertGreaterEqual(up["paste_enter_gap"], ClipboardBackend.PASTE_ENTER_GAP_1X)
        self.assertGreaterEqual(up["copy_settle"], ClipboardBackend.COPY_SETTLE_1X)
        self.assertGreaterEqual(up["inter_code_gap"], ClipboardBackend.GAP_1X)


class TestAutomationManagerSelectorWiring(unittest.TestCase):
    """El facade respeta metodo / velocidad / Modo seguro del config."""

    class _Cfg:
        def __init__(self, **kw):
            self._d = {"paste_mode": "typing", "speed_multiplier": 1.0,
                       "typing_speed": DEFAULT_TYPING_SPEED, "safe_mode": False}
            self._d.update(kw)

        def get(self, key, default=None):
            return self._d.get(key, default)

    def test_safe_mode_on_forces_clipboard_ultra(self):
        am = AutomationManager(self._Cfg(paste_mode="typing", safe_mode=True), _StubInventory())
        backend = am._make_backend("typing")
        self.assertIsInstance(backend, ClipboardBackend)
        self.assertEqual(backend.pacing["paste_enter_gap"], ClipboardBackend.ULTRA_SAFE["paste_enter_gap"])

    def test_safe_mode_off_uses_typing_profile(self):
        am = AutomationManager(self._Cfg(paste_mode="typing", safe_mode=False, typing_speed=2), _StubInventory())
        backend = am._make_backend("typing")
        self.assertIsInstance(backend, TypingBackend)
        self.assertEqual(backend.pacing["char_delay"], typing_profile(2)["char_delay"])
        self.assertEqual(backend.inter_code_gap, typing_profile(2)["gap"])

    def test_clipboard_method_uses_ultra_regardless_of_safe_flag(self):
        am = AutomationManager(self._Cfg(paste_mode="clipboard", safe_mode=False), _StubInventory())
        backend = am._make_backend("clipboard")
        self.assertIsInstance(backend, ClipboardBackend)
        self.assertEqual(backend.inter_code_gap, ClipboardBackend.ULTRA_SAFE["inter_code_gap"])


if __name__ == "__main__":
    unittest.main()
