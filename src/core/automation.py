import time
import threading
from dataclasses import dataclass, field
from pynput import keyboard
import pyperclip

from src.logger import logger

# ---------------------------------------------------------------------------
# Inyeccion ATOMICA de Ctrl+V (un unico SendInput) - Paso 2 (2026-09-10)
# ---------------------------------------------------------------------------
try:
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _KBDINPUT(_ctypes.Structure):
        _fields_ = [("wVk", _wintypes.WORD), ("wScan", _wintypes.WORD),
                    ("dwFlags", _wintypes.DWORD), ("time", _wintypes.DWORD),
                    ("dwExtraInfo", _ctypes.POINTER(_ctypes.c_ulong))]

    class _MOUSEINPUT(_ctypes.Structure):
        _fields_ = [("dx", _wintypes.LONG), ("dy", _wintypes.LONG),
                    ("mouseData", _wintypes.DWORD), ("dwFlags", _wintypes.DWORD),
                    ("time", _wintypes.DWORD),
                    ("dwExtraInfo", _ctypes.POINTER(_ctypes.c_ulong))]

    class _INPUTunion(_ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KBDINPUT)]

    class _INPUT(_ctypes.Structure):
        _fields_ = [("type", _wintypes.DWORD), ("u", _INPUTunion)]

    _VK_CONTROL = 0x11
    _VK_V = 0x56
    _KEYUP = 0x0002

    def _send_ctrl_v_atomic():
        """Ctrl down, V down, V up, Ctrl up en UNA sola llamada SendInput."""
        arr = (_INPUT * 4)()
        specs = [(_VK_CONTROL, 0), (_VK_V, 0), (_VK_V, _KEYUP), (_VK_CONTROL, _KEYUP)]
        for i, (vk, flags) in enumerate(specs):
            arr[i].type = 1  # INPUT_KEYBOARD
            arr[i].u.ki.wVk = vk
            arr[i].u.ki.wScan = 0
            arr[i].u.ki.dwFlags = flags
            arr[i].u.ki.time = 0
            arr[i].u.ki.dwExtraInfo = None
        sent = _ctypes.windll.user32.SendInput(4, arr, _ctypes.sizeof(_INPUT))
        if sent != 4:
            raise RuntimeError("SendInput devolvio %s/4 eventos" % sent)
except Exception:
    _send_ctrl_v_atomic = None



# ===========================================================================
# Motor de exportación — Stock Cellular Center V8.1 (perfil calibrado)
# ===========================================================================
#
# Calibración de ritmo (Auditoría 2026-09-08 / Perfil Rápido):
#   - COPY_SETTLE: 10 ms (piso 5 ms)
#   - CTRL_HOLD: 8 ms
#   - KEY_HOLD: 3 ms
#   - PASTE_ENTER_GAP: 35 ms (piso 30 ms)
#   - ENTER_SETTLE: 8 ms (piso 5 ms)
#   - GAP (inter-código): 25 ms (piso 20 ms)
#
# RENDIMIENTO ESTIMADO:
#   - ~10 a 11 códigos/s en Clipboard.
#   - 1800 códigos en ~2m 45s a ~3m 05s con 100% de integridad.
# ===========================================================================


class ExportState:
    """Estados posibles del motor de exportación."""
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass
class ExportReport:
    """Resultado real de una exportación (nunca miente)."""
    state: str = ExportState.IDLE
    total: int = 0
    sent: int = 0
    error: str = None
    elapsed_s: float = 0.0
    codes_per_second: float = 0.0
    applied_multiplier: float = 1.0
    requested_multiplier: float = 1.0
    backend: str = ""
    paused_count: int = 0
    verify_failures: int = 0

    @property
    def completed(self):
        return self.state == ExportState.COMPLETED


# ---------------------------------------------------------------------------
# Modelo de ritmo (puro, testeable sin wall-clock)
# ---------------------------------------------------------------------------

def _clamp_multiplier(multiplier, max_safe):
    try:
        mult = float(multiplier)
    except (TypeError, ValueError):
        mult = 1.0
    return max(0.1, min(mult, max_safe))


# Longitud típica de código para el cálculo del multiplicador efectivo
TYPICAL_CODE_LEN = 10


def _schedule_total_1x(backend_name):
    """Suma de delays del schedule a 1x (referencia para el mult. efectivo)."""
    if backend_name == "clipboard":
        return (ClipboardBackend.COPY_SETTLE_1X
                + ClipboardBackend.CTRL_HOLD + 2 * ClipboardBackend.KEY_HOLD
                + ClipboardBackend.PASTE_ENTER_GAP_1X
                + ClipboardBackend.ENTER_SETTLE_1X
                + ClipboardBackend.GAP_1X)
    return (TypingBackend.CHAR_DELAY_1X * TYPICAL_CODE_LEN
            + TypingBackend.ENTER_SETTLE_1X
            + TypingBackend.GAP_1X)


def compute_pacing(multiplier, backend_name):
    """Devuelve los delays a aplicar para una velocidad solicitada.

    Aplica perfil calibrado rápido garantizando que los gaps del receptor
    no caigan por debajo de los pisos seguros verificados.
    """
    if backend_name == "clipboard":
        mult = _clamp_multiplier(multiplier, ClipboardBackend.MAX_MULT)
        pacing = {
            "copy_settle": max(ClipboardBackend.COPY_SETTLE_FLOOR,
                               ClipboardBackend.COPY_SETTLE_1X / mult),
            "paste_enter_gap": ClipboardBackend.PASTE_ENTER_GAP_1X,
            "enter_settle": max(ClipboardBackend.ENTER_FLOOR,
                                ClipboardBackend.ENTER_SETTLE_1X / mult),
            "inter_code_gap": ClipboardBackend.GAP_1X,
            "char_delay": 0.0,
            "ctrl_hold": ClipboardBackend.CTRL_HOLD,
            "key_hold": ClipboardBackend.KEY_HOLD,
            "gap_floor": ClipboardBackend.GAP_FLOOR,
            "paste_enter_floor": ClipboardBackend.PASTE_ENTER_FLOOR,
        }
    else:  # typing
        mult = _clamp_multiplier(multiplier, TypingBackend.MAX_MULT)
        pacing = {
            "char_delay": max(TypingBackend.CHAR_FLOOR, TypingBackend.CHAR_DELAY_1X / mult),
            "enter_settle": max(TypingBackend.ENTER_FLOOR,
                                TypingBackend.ENTER_SETTLE_1X / mult),
            "inter_code_gap": TypingBackend.GAP_1X,
            "copy_settle": 0.0,
            "paste_enter_gap": 0.0,
            "ctrl_hold": 0.0,
            "key_hold": 0.0,
            "gap_floor": TypingBackend.GAP_FLOOR,
            "paste_enter_floor": 0.0,
        }
    total_1x = _schedule_total_1x(backend_name)
    total_actual = (pacing["copy_settle"] + pacing["paste_enter_gap"]
                    + pacing["enter_settle"] + pacing["inter_code_gap"]
                    + pacing["ctrl_hold"] + 2 * pacing["key_hold"]
                    + pacing["char_delay"] * TYPICAL_CODE_LEN)
    applied = total_1x / total_actual if total_actual > 0 else 1.0
    requested = _clamp_multiplier(multiplier, 999.0)
    pacing["applied_multiplier"] = round(max(0.1, min(applied, requested)), 2)
    return pacing


# ---------------------------------------------------------------------------
# Perfiles de velocidad de TYPING (modo rapido) - calibrados con pruebas REALES
# ---------------------------------------------------------------------------
#
# Medicion REAL (receptor GUI, sesion interactiva de Windows, dataset de 1800
# codigos sinteticos, backends reales del repo):
#
#   Perfil                                   cod/s   1800 codigos   integridad
#   char .005 / enter .015 / gap .020         ~11      ~2m40s        1800/1800
#   char .002 / enter .010 / gap .015         ~19      ~1m35s        1800/1800
#   char 0    / enter .008 / gap .010         ~43      ~42s          1800/1800
#   char 0    / enter .005 / gap .005         ~66      ~27s          1800/1800 (x4)
#   char 0    / enter .003 / gap .005         ~75      ~24s          1691/1800  FALLA
#   char 0    / enter .002 / gap .002         ~84      ~21s           700/1800  FALLA
#
# Maximo ESTABLE verificado: "Muy rapida" (~66 cod/s). Por encima de ~70 cod/s
# el receptor real empieza a perder codigos.
TYPING_PROFILES = [
    {"name": "Lenta", "char_delay": 0.005, "enter_settle": 0.015, "gap": 0.020},
    {"name": "Normal", "char_delay": 0.002, "enter_settle": 0.010, "gap": 0.015},
    {"name": "Rapida", "char_delay": 0.0, "enter_settle": 0.008, "gap": 0.010},
    {"name": "Muy rapida", "char_delay": 0.0, "enter_settle": 0.005, "gap": 0.005},
]

TYPING_SPEED_MAX = len(TYPING_PROFILES)
DEFAULT_TYPING_SPEED = 3  # "Rapida"


def clamp_typing_speed(speed):
    """Normaliza una velocidad de Typing al rango valido (1..TYPING_SPEED_MAX)."""
    try:
        s = int(speed)
    except (TypeError, ValueError):
        s = DEFAULT_TYPING_SPEED
    return max(1, min(s, TYPING_SPEED_MAX))


def typing_profile(speed):
    """Perfil (dict) para una velocidad 1..TYPING_SPEED_MAX."""
    return TYPING_PROFILES[clamp_typing_speed(speed) - 1]


def selector_semantics(method, safe_mode):
    """Fuente unica de verdad del selector de la UI.

    - metodo "typing" sin Modo seguro -> selector de velocidad HABILITADO,
      backend de tecleo con el perfil elegido.
    - metodo "clipboard" o Modo seguro -> selector DESHABILITADO y backend de
      Portapapeles en perfil ULTRA SAFE.
    """
    safe = bool(safe_mode) or (method == "clipboard")
    return {
        "speed_enabled": (not safe) and (method == "typing"),
        "use_safe_mode": safe,
        "effective_method": "clipboard" if safe else "typing",
    }


def build_backend(method, speed=DEFAULT_TYPING_SPEED, safe_mode=False,
                  controller=None, clipboard=None, sleep_fn=time.sleep):
    """Construye el backend REAL segun metodo / velocidad / Modo seguro."""
    sem = selector_semantics(method, safe_mode)
    if sem["effective_method"] == "clipboard":
        return ClipboardBackend(controller=controller, clipboard=clipboard,
                                sleep_fn=sleep_fn, ultra_safe=True)
    prof = typing_profile(speed)
    backend = TypingBackend(controller=controller, sleep_fn=sleep_fn, multiplier=1.0)
    backend.pacing = {
        "char_delay": prof["char_delay"],
        "enter_settle": prof["enter_settle"],
        "inter_code_gap": prof["gap"],
        "applied_multiplier": 1.0,
        "copy_settle": 0.0,
        "paste_enter_gap": 0.0,
        "ctrl_hold": 0.0,
        "key_hold": 0.0,
        "gap_floor": TypingBackend.GAP_FLOOR,
        "paste_enter_floor": 0.0,
    }
    backend.inter_code_gap = prof["gap"]
    backend.applied_multiplier = 1.0
    return backend


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class ExportBackend:
    """Interfaz común de un mecanismo de entrega de códigos.

    El motor sólo conoce: name, send_code(code), verify(code), inter_code_gap,
    applied_multiplier. Los detalles de pynput/pyperclip quedan encapsulados
    en cada implementación.
    """

    name = "base"
    inter_code_gap = 0.0
    applied_multiplier = 1.0

    def send_code(self, code):
        """Entrega un código COMPLETO (contenido + Enter). Atómico para el
        motor: pausa/cancelación se aplican entre códigos, nunca a mitad."""
        raise NotImplementedError

    def verify(self, code):
        """Verificación barata post-entrega (por defecto: no-op)."""
        return True


class TypingBackend(ExportBackend):
    """Entrega por tecleo carácter por carácter (SendInput vía pynput)."""

    name = "typing"
    CHAR_DELAY_1X = 0.020
    ENTER_SETTLE_1X = 0.030
    GAP_1X = 0.050
    CHAR_FLOOR = 0.015
    ENTER_FLOOR = 0.020
    GAP_FLOOR = 0.050
    MAX_MULT = 3.0

    def __init__(self, controller=None, sleep_fn=time.sleep, multiplier=1.0):
        self._controller = controller if controller is not None else keyboard.Controller()
        self._sleep = sleep_fn
        self.pacing = compute_pacing(multiplier, self.name)
        self.inter_code_gap = self.pacing["inter_code_gap"]
        self.applied_multiplier = self.pacing["applied_multiplier"]

    def send_code(self, code):
        p = self.pacing
        ctrl = self._controller
        for ch in code:
            ctrl.type(ch)
            self._sleep(p["char_delay"])
        ctrl.press(keyboard.Key.enter)
        ctrl.release(keyboard.Key.enter)
        self._sleep(p["enter_settle"])


class ClipboardBackend(ExportBackend):
    """Entrega por portapapeles (pyperclip) + Ctrl+V con perfil rápido calibrado."""

    name = "clipboard"
    COPY_SETTLE_1X = 0.010
    COPY_SETTLE_FLOOR = 0.005
    CTRL_HOLD = 0.008
    KEY_HOLD = 0.003
    PASTE_ENTER_GAP_1X = 0.035
    PASTE_ENTER_FLOOR = 0.030
    ENTER_SETTLE_1X = 0.008
    ENTER_FLOOR = 0.005
    GAP_1X = 0.025
    GAP_FLOOR = 0.020
    COPY_RETRIES = 3
    RETRY_BACKOFF = 0.100
    PASTE_RETRIES = 3
    MAX_MULT = 3.0

    # Perfil ULTRA SAFE (metodo Portapapeles / Modo seguro): maxima fiabilidad,
    # SIN multiplicador. Validado con pruebas REALES (1800/1800 codigos).
    ULTRA_SAFE = {
        "copy_settle": 0.050,
        "ctrl_hold": 0.020,
        "key_hold": 0.010,
        "paste_enter_gap": 0.120,
        "enter_settle": 0.040,
        "inter_code_gap": 0.080,
    }

    def __init__(self, controller=None, clipboard=None, sleep_fn=time.sleep, multiplier=1.0, ultra_safe=False):
        self._controller = controller if controller is not None else keyboard.Controller()
        self._clipboard = clipboard if clipboard is not None else pyperclip
        self._sleep = sleep_fn
        if ultra_safe:
            self.pacing = dict(self.ULTRA_SAFE)
            self.pacing["applied_multiplier"] = 1.0
            self.inter_code_gap = self.pacing["inter_code_gap"]
            self.applied_multiplier = 1.0
        else:
            self.pacing = compute_pacing(multiplier, self.name)
            self.inter_code_gap = self.pacing["inter_code_gap"]
            self.applied_multiplier = self.pacing["applied_multiplier"]

    def _copy_with_retry(self, code):
        last_exc = None
        for attempt in range(1, self.COPY_RETRIES + 1):
            try:
                self._clipboard.copy(code)
            except Exception as exc:  # contención: OpenClipboard falló
                last_exc = exc
                if attempt >= self.COPY_RETRIES:
                    break
                self._sleep(self.RETRY_BACKOFF)
                continue
            # verificación: el clipboard contiene exactamente lo copiado
            try:
                if self._clipboard.paste() == code:
                    return
            except Exception:
                pass
            last_exc = RuntimeError(f"clipboard no verificable para {code!r}")
            if attempt < self.COPY_RETRIES:
                self._sleep(self.RETRY_BACKOFF)
        raise RuntimeError(f"no se pudo copiar {code!r} al clipboard: {last_exc}")

    def _ctrl_v(self):
        """Inyecta Ctrl+V. Con un controller pynput REAL usa un unico SendInput
        atomico (Ctrl down, V down, V up, Ctrl up); con controllers simulados
        (tests) emula press/release con las esperas calibradas, para seguir
        siendo observable/determinista."""
        if _send_ctrl_v_atomic is not None and isinstance(self._controller, keyboard.Controller):
            _send_ctrl_v_atomic()
            return
        c = self._controller
        c.press(keyboard.Key.ctrl)
        self._sleep(self.pacing["ctrl_hold"])
        c.press("v")
        self._sleep(self.pacing["key_hold"])
        c.release("v")
        self._sleep(self.pacing["key_hold"])
        c.release(keyboard.Key.ctrl)

    def send_code(self, code):
        p = self.pacing
        ctrl = self._controller
        self._copy_with_retry(code)
        self._sleep(p["copy_settle"])
        self._ctrl_v()
        self._sleep(p["paste_enter_gap"])
        # Verificacion de paste (no ciega): si el clipboard fue reemplazado
        # justo despues de pegar, el receptor pudo NO recibir el codigo.
        # Re-copiamos y re-pegamos SOLO si la verificacion lo detecta.
        for _ in range(self.PASTE_RETRIES):
            if self.verify(code):
                break
            self._copy_with_retry(code)
            self._sleep(p["copy_settle"])
            self._ctrl_v()
            self._sleep(p["paste_enter_gap"])
        ctrl.press(keyboard.Key.enter)
        ctrl.release(keyboard.Key.enter)
        self._sleep(p["enter_settle"])

    def verify(self, code):
        try:
            return self._clipboard.paste() == code
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Motor (cola de exportación controlada)
# ---------------------------------------------------------------------------

class ExportEngine:
    """Controla el flujo: estados, pausa (F8), cancelación, ritmo y reporte."""

    def __init__(self, stop_event=None, pause_event=None, sleep_fn=time.sleep):
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._pause = pause_event if pause_event is not None else threading.Event()
        self._pause.set()  # set = habilitado para continuar; clear = pausado
        self.state = ExportState.IDLE
        self.report = None

    # -- control -----------------------------------------------------------

    def pause(self):
        with self._lock:
            if self.state == ExportState.RUNNING:
                self.state = ExportState.PAUSED
                self._pause.clear()
                return True
            return False

    def resume(self):
        with self._lock:
            if self.state == ExportState.PAUSED:
                self.state = ExportState.RUNNING
                self._pause.set()
                return True
            return False

    def cancel(self):
        self._stop.set()

    @property
    def paused(self):
        return not self._pause.is_set()

    # -- ejecución ----------------------------------------------------------

    def run(self, codes, backend, progress_callback=None, multiplier=1.0):
        with self._lock:
            if self.state == ExportState.RUNNING:
                raise RuntimeError("ya hay una exportación en curso")
            self.state = ExportState.RUNNING

        total = len(codes)
        sent = 0
        error = None
        paused_count = 0
        verify_failures = 0
        t0 = time.perf_counter()
        try:
            for code in codes:
                if self._stop.is_set():
                    self.state = ExportState.CANCELLED
                    break
                if not self._pause.is_set():
                    paused_count += 1
                self._pause.wait()  # F8: bloquea entre códigos
                if self._stop.is_set():
                    self.state = ExportState.CANCELLED
                    break
                try:
                    backend.send_code(code)
                except Exception as exc:
                    error = exc
                    self.state = ExportState.ERROR
                    break
                if not backend.verify(code):
                    verify_failures += 1
                    logger.warning(
                        "Verificación post-entrega falló para %r "
                        "(clipboard reemplazado antes de procesarse)", code)
                sent += 1
                if progress_callback:
                    progress_callback(code, sent, total)
                self._sleep(backend.inter_code_gap)
            else:
                if self.state == ExportState.RUNNING and sent == total:
                    self.state = ExportState.COMPLETED
        finally:
            elapsed = time.perf_counter() - t0
            if self.state == ExportState.RUNNING:
                self.state = ExportState.CANCELLED if sent < total else ExportState.COMPLETED

        report = ExportReport(
            state=self.state,
            total=total,
            sent=sent,
            error=str(error) if error else None,
            elapsed_s=round(elapsed, 3),
            codes_per_second=round(sent / elapsed, 2) if elapsed > 0 else 0.0,
            applied_multiplier=getattr(backend, "applied_multiplier", 1.0),
            requested_multiplier=round(float(multiplier), 2),
            backend=getattr(backend, "name", "?"),
            paused_count=paused_count,
            verify_failures=verify_failures,
        )
        self.report = report
        return report


# ---------------------------------------------------------------------------
# Construcción de la lista a exportar (pura, testeable)
# ---------------------------------------------------------------------------

def build_export_list(scanned_items, family, family_map, excluded=frozenset()):
    """Construye la lista de códigos en orden cronológico de escaneo."""
    scans = []
    family = (family or "").upper()
    for code, positions in scanned_items.items():
        if code in excluded:
            continue
        item_fam = family_map.get(code, "").upper()
        if item_fam.startswith(family):
            for pos in positions:
                scans.append((pos, code))
    scans.sort()
    return [code for _, code in scans]


# ===========================================================================
# Facade (compatibilidad con la API histórica de AutomationManager)
# ===========================================================================

class AutomationManager:
    def __init__(self, config_manager, inventory_manager, backend_factory=None):
        self.config = config_manager
        self.inventory = inventory_manager
        self.controller = keyboard.Controller()
        self.listener = None
        self.stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self.is_paused = False
        self._backend_factory = backend_factory
        self._engine = ExportEngine(stop_event=self.stop_event, pause_event=self._pause_event)

        # QR especial para borrar el último escaneado
        self.QR_DELETE_CODE = "DEL_LAST_SCAN_QR"

    # -- listener global (F2, F8) ------------------------------------------

    def start_global_listener(self, on_f2_callback):
        """Inicia el escucha de teclas globales (F2, F8)."""

        def on_press(key):
            try:
                if key == keyboard.Key.f2:
                    on_f2_callback()
                elif key == keyboard.Key.f8:
                    self.toggle_pause()
            except AttributeError:
                pass

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.start()

    def toggle_pause(self):
        """F8: RUNNING -> PAUSED -> RUNNING. Termina el código en curso."""
        p = self.inventory.config.get("parent_app")
        if self._engine.pause():
            self.is_paused = True
            logger.info("Exportación pausada (F8)")
            if p:
                p.show_toast("Vaciado: PAUSADO", mtype="warning", duration=2000, use_history=False)
        elif self._engine.resume():
            self.is_paused = False
            logger.info("Exportación reanudada (F8)")
            if p:
                p.show_toast("Vaciado: REANUDADO", mtype="success", duration=2000, use_history=False)

    # -- backends ------------------------------------------------------------

    def _make_backend(self, mode, multiplier=None):
        if self._backend_factory is not None:
            backend = self._backend_factory(mode)
            backend.inter_code_gap = getattr(backend, "inter_code_gap", 0.0)
            return backend
        safe_mode = bool(self.config.get("safe_mode", False))
        speed = self.config.get("typing_speed", DEFAULT_TYPING_SPEED)
        return build_backend(mode, speed, safe_mode, controller=self.controller)

    # -- métodos de compatibilidad -------------------------------------------

    def type_string(self, text):
        """Simula el tecleo de un string (compatibilidad; sin control de flujo)."""
        TypingBackend(
            controller=self.controller,
            multiplier=self.config.get("speed_multiplier", 1.0),
        ).send_code(text)

    def paste_string(self, text):
        """Simula pegar el texto con Ctrl+V (compatibilidad)."""
        ClipboardBackend(
            controller=self.controller,
            multiplier=self.config.get("speed_multiplier", 1.0),
        ).send_code(text)

    # -- exportación ----------------------------------------------------------

    def process_export(self, code_list, mode="typing", progress_callback=None):
        """Procesa una lista de códigos para exportar."""
        self.stop_event.clear()
        self._pause_event.set()
        self.is_paused = False
        multiplier = self.config.get("speed_multiplier", 1.0)
        backend = self._make_backend(mode, multiplier)
        total = len(code_list)

        logger.info(
            "Exportación iniciada: total=%d, modo=%s, velocidad_solicitada=%.1fx",
            total, backend.name, float(multiplier),
        )
        report = self._engine.run(
            code_list, backend,
            progress_callback=progress_callback,
            multiplier=multiplier,
        )
        self._log_report(report)
        return report

    def _log_report(self, report):
        if report.state == ExportState.COMPLETED:
            logger.info(
                "Exportación completada: enviados=%d/%d, %.1fs, %.1f códigos/s, "
                "velocidad_aplicada=%.2fx (solicitada %.2fx)%s",
                report.sent, report.total, report.elapsed_s,
                report.codes_per_second, report.applied_multiplier,
                report.requested_multiplier,
                f", verificaciones_fallidas={report.verify_failures}" if report.verify_failures else "",
            )
        elif report.state == ExportState.CANCELLED:
            logger.info(
                "Exportación cancelada: enviados=%d/%d, %.1fs",
                report.sent, report.total, report.elapsed_s,
            )
        elif report.state == ExportState.ERROR:
            logger.error(
                "Exportación interrumpida por error: enviados=%d/%d, error=%s",
                report.sent, report.total, report.error,
            )
        else:
            logger.info("Exportación finalizada con estado %s: enviados=%d/%d",
                        report.state, report.sent, report.total)

    def export_data(self):
        """Inicia el proceso de vaciado con cuenta regresiva (flujo completo)."""
        p = self.inventory.config.get("parent_app")
        if not p:
            return

        all_codes = build_export_list(
            self.inventory.scanned_items,
            "",  # sin filtro de familia: vaciado total
            self.inventory.family_map,
            frozenset(),
        )
        if not all_codes:
            p.show_toast("No hay códigos para exportar.", mtype="error")
            return

        def run_export():
            delay = self.config.get("export_delay_seconds", 10)
            for i in range(delay, 0, -1):
                if self.stop_event.is_set():
                    return
                p.show_toast(f"Vaciado en {i}s... ¡Prepara la ventana!",
                             mtype="info", duration=1000, use_history=False)
                time.sleep(1)

            p.show_toast("Iniciando Vaciado...", mtype="success", duration=2000)
            mode = self.config.get("paste_mode", "typing")
            try:
                report = self.process_export(all_codes, mode=mode)
            except Exception:
                logger.exception("Error fatal durante exportación")
                p.show_toast("Vaciado interrumpido por error", mtype="error")
                return

            if report.state == ExportState.COMPLETED:
                p.show_toast(f"Vaciado Completo ({report.sent}/{report.total})", mtype="success")
            elif report.state == ExportState.CANCELLED:
                p.show_toast(f"Vaciado cancelado ({report.sent}/{report.total})", mtype="warning")
            elif report.state == ExportState.ERROR:
                p.show_toast(f"Vaciado interrumpido por error ({report.sent}/{report.total})",
                             mtype="error")
            else:
                p.show_toast(f"Vaciado {report.state} ({report.sent}/{report.total})", mtype="warning")

        threading.Thread(target=run_export, daemon=True).start()

    # -- misc ---------------------------------------------------------------

    def check_qr_command(self, scanned_code):
        """Verifica si el código escaneado es un comando (ej. borrar último)."""
        if scanned_code == self.QR_DELETE_CODE:
            return self.inventory.delete_last()
        return False

    def stop_automation(self):
        self.stop_event.set()
