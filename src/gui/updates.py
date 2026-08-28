"""
Proyección de vistas de UI — Stock Cellular Center V8.0 (Fase B, paso 1)

Módulo PURAMENTE funcional: sin Tkinter, sin widgets, sin efectos secundarios.
Calcula, a partir del modelo de inventario (InventoryManager o cualquier
objeto con la misma interfaz), el contenido exacto de:

  - tabla de productos escaneados  (scanned)
  - tabla maestra                  (master)
  - ventana de diferencias         (diff)
  - métricas del panel resumen     (metrics)

Dos caminos equivalentes:
  * build_full_view(model, options) -> proyección completa (rebuild).
  * apply_event(view, event, model, options) -> proyección incremental
    dirigida por UN evento de escaneo ya aplicado al modelo; devuelve la
    vista nueva y las acciones de UI (diff_views) para alcanzarla.

Invariante de oro (verificado por tests/test_fase_b_projection.py):
para cualquier secuencia de eventos ya aplicada al modelo, aplicar
apply_event() paso a paso produce una vista IDÉNTICA a build_full_view()
sobre el modelo final.

apply_actions() reproduce una vista a partir de las acciones generadas;
tiene la misma semántica que tendrá el aplicador Tk del paso 3.

Ninguna función modifica el modelo: solo lo lee.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

# ---------------------------------------------------------------------------
# Constantes (idénticas a los valores usados por main.py / inventory.py)
# ---------------------------------------------------------------------------

# Modos de orden del selector de la tabla escaneada
SORT_LAST_TOP = "Último arriba"
SORT_LAST_BOTTOM = "Último abajo"
SORT_ALPHA = "Alfabético"
SORT_QTY = "Cantidad"
SORT_SCAN = "Escaneo"

# Modos cuyo orden visual deriva directamente de scan_sequence
SEQUENTIAL_SORTS = (SORT_LAST_TOP, SORT_LAST_BOTTOM, SORT_SCAN)

# Tablas
TABLE_SCANNED = "scanned"
TABLE_MASTER = "master"
TABLE_DIFF = "diff"

# Marcadores visuales (mismos caracteres que main.py)
FOLD_OPEN = "▼ "
FOLD_CLOSED = "▶ "
WAITING_REPLACEMENT = "[ESPERANDO REEMPLAZO]"
CHECKMARK = "✓"
EXPORT_EXCLUDED = "☑"
EXPORT_INCLUDED = "☐"

# Colores de filas QR (idénticos a _refresh_tables de main.py)
QR_COLORS = {
    "caja": ("CAJA", ("#333333", "#ffffff")),
    "mueble": ("MUEBLE", ("#1f4e78", "#ffffff")),
    "vidriera": ("VIDRIERA", ("#7030a0", "#ffffff")),
}

DEFAULT_TABLE_BG = "#242424"
DEFAULT_TABLE_FG = "#ffffff"
DEFAULT_LOCATION = "Desconocida"


# ---------------------------------------------------------------------------
# Estructuras de datos (inmutables)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RowSpec:
    """Especificación completa de una fila de Treeview.

    key: identificador estable de la fila (para reconciliación):
      - scanned secuencial : "p:<pos>"  (una fila por posición)
      - scanned agrupado   : "g:<code>"
      - master             : "m:<code>"
      - diff               : "d:<code>"
    values: tupla de valores de columnas (igual que en main.py; los de
      diff llevan ints para expected/scanned, como el código actual).
    text:  texto de la columna #0 del Treeview (posición 1-indexed en
      scan_sequence) o None si la tabla no usa esa columna.
    colors: par (fondo, fuente).
    bold:  True solo para filas QR.
    """
    table: str
    key: str
    values: Tuple[object, ...]
    text: Optional[str]
    colors: Tuple[str, str]
    bold: bool


@dataclass(frozen=True)
class Metrics:
    """Valores del panel resumen, idénticos a _update_all_ui de main.py."""
    scanned_count: int
    expected_count: int
    diff_net: int
    relevant_diffs: int
    percent: float


@dataclass(frozen=True)
class ScanView:
    """Contenido completo de las tres tablas + métricas."""
    scanned: Tuple[RowSpec, ...]
    master: Tuple[RowSpec, ...]
    diff: Tuple[RowSpec, ...]
    metrics: Metrics


@dataclass(frozen=True)
class ViewOptions:
    """Opciones de vista que en main.py viven en la app (no en el modelo).

    sort_mode: uno de SORT_* (default: último abajo).
    collapsed_containers: QRs plegados (doble clic).
    excluded_from_export: SKUs marcados como "No exportar" (☑ en diff).
    location_resolver: callable sku -> str usado por la columna "Posible
      Ubicación" de la ventana de diferencias (main.py: get_possible_location).
    """
    sort_mode: str = SORT_LAST_BOTTOM
    collapsed_containers: frozenset = field(default_factory=frozenset)
    excluded_from_export: frozenset = field(default_factory=frozenset)
    location_resolver: Optional[Callable[[str], str]] = None


@dataclass(frozen=True)
class Action:
    """Acción de UI derivada de la diferencia entre dos vistas.

    op: "insert" | "update" | "delete" | "move"
      - insert: crear fila `row` en el índice `index` de la tabla.
      - update: reemplazar el contenido de la fila `key` por `row`.
      - move:   reposicionar la fila `key` al índice `index` (sin tocar contenido).
      - delete: eliminar la fila `key`.
    """
    op: str
    table: str
    key: str
    row: Optional[RowSpec] = None
    index: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers de configuración
# ---------------------------------------------------------------------------

def _cfg(model, key, default):
    cfg = getattr(model, "config", None)
    if cfg is None:
        return default
    return cfg.get(key, default)


def _pos_from_key(key: str) -> int:
    """Extrae la posición de una key de fila secuencial "p:<pos>"."""
    return int(key.split(":", 1)[1])


# ---------------------------------------------------------------------------
# Proyección de filas individuales (compartidas por rebuild e incremental)
# ---------------------------------------------------------------------------

def _scanned_row_for(model, options: ViewOptions, idx: int) -> Optional[RowSpec]:
    """Proyecta la fila de la posición idx de scan_sequence.

    Devuelve None cuando la fila no debe existir en la vista (producto
    dentro de un contenedor colapsado).
    """
    seq = model.scan_sequence
    if idx < 0 or idx >= len(seq):
        return None

    code = seq[idx]
    pos = idx + 1
    code_display = WAITING_REPLACEMENT if idx == model.qr_replace_index else code
    is_qr = model.is_qr_code(code)

    if is_qr:
        c_type = model.get_code_type(code)
        fam, tag = QR_COLORS.get(c_type, ("CAJA", QR_COLORS["caja"][1]))
        is_collapsed = code in options.collapsed_containers
        fold_icon = FOLD_CLOSED if is_collapsed else FOLD_OPEN
        status = model.get_container_status(code, seq)
        qty_str = status["display_str"]
        return RowSpec(
            TABLE_SCANNED, f"p:{pos}",
            (fam, f"{fold_icon}{code_display}", qty_str),
            str(pos), tag, True,
        )

    # Producto: oculto si su contenedor activo está colapsado
    box, sec = model.get_containers_for_index(seq, idx)
    if (box and box in options.collapsed_containers) or (sec and sec in options.collapsed_containers):
        return None

    fam = model.family_map.get(code, "??")
    positions = model.scanned_items.get(code, [])
    total_qty = len(positions)
    is_last = (pos == max(positions)) if positions else False

    if is_last:
        expected = model.original_quantities.get(code, 0)
        colors = model.get_row_color(expected, total_qty)
        qty_str = str(total_qty)
    else:
        colors = (_cfg(model, "table_bg", DEFAULT_TABLE_BG), _cfg(model, "table_fg", DEFAULT_TABLE_FG))
        qty_str = ""

    return RowSpec(
        TABLE_SCANNED, f"p:{pos}",
        (fam, code_display, qty_str),
        str(pos), colors, False,
    )


def _scanned_view(model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Proyección completa de la tabla escaneada (fiel a _refresh_tables)."""
    seq = model.scan_sequence
    sort_mode = options.sort_mode

    if sort_mode in SEQUENTIAL_SORTS:
        if sort_mode in (SORT_LAST_TOP, SORT_SCAN):
            indices = range(len(seq) - 1, -1, -1)
        else:
            indices = range(len(seq))
        rows = []
        for idx in indices:
            row = _scanned_row_for(model, options, idx)
            if row is not None:
                rows.append(row)
        return tuple(rows)

    # Modos agrupados (Alfabético / Cantidad): una fila por SKU, sin QRs
    groups = {}
    for code, positions in model.scanned_items.items():
        if model.is_qr_code(code):
            continue
        groups[code] = (model.family_map.get(code, "??"), len(positions))

    items = list(groups.items())
    if sort_mode == SORT_ALPHA:
        items.sort(key=lambda x: x[0])
    elif sort_mode == SORT_QTY:
        items.sort(key=lambda x: x[1][1], reverse=True)

    rows = []
    for code, (fam, qty) in items:
        expected = model.original_quantities.get(code, 0)
        colors = model.get_row_color(expected, qty)
        rows.append(RowSpec(
            TABLE_SCANNED, f"g:{code}",
            (fam, code, str(qty)),
            None, colors, False,
        ))
    return tuple(rows)


def _master_view(model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Proyección completa de la tabla maestra (fiel a _load_master_table)."""
    rows = []
    for code, desc, qty in model.stock_data:
        scanned = len(model.scanned_items.get(code, []))
        colors = model.get_row_color(qty, scanned)
        rows.append(RowSpec(
            TABLE_MASTER, f"m:{code}",
            (code, desc, f"{qty} ({scanned})"),
            None, colors, False,
        ))
    return tuple(rows)


def _diff_view(model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Proyección completa de la ventana de diferencias (fiel a _refresh_diff_window)."""
    resolver = options.location_resolver if options.location_resolver else (lambda c: DEFAULT_LOCATION)

    def no_exp(code):
        return EXPORT_EXCLUDED if code in options.excluded_from_export else EXPORT_INCLUDED

    rows = []

    # 1. Faltantes (solo códigos del CSV maestro)
    for code, desc, expected in model.stock_data:
        scanned = len(model.scanned_items.get(code, []))
        if scanned < expected:
            diff = expected - scanned
            colors = model.get_row_color(expected, scanned)
            rows.append(RowSpec(
                TABLE_DIFF, f"d:{code}",
                (no_exp(code), code, desc, expected, scanned, f"-{diff}", "FALTANTE", resolver(code)),
                None, colors, False,
            ))

    # 2. Sobrantes (cualquier código escaneado no QR con más unidades que el esperado)
    for code, positions in model.scanned_items.items():
        if model.is_qr_code(code):
            continue
        scanned = len(positions)
        expected = model.original_quantities.get(code, 0)
        if scanned > expected:
            diff = scanned - expected
            colors = model.get_row_color(expected, scanned)
            desc = next((d for c, d, _e in model.stock_data if c == code), "")
            rows.append(RowSpec(
                TABLE_DIFF, f"d:{code}",
                (no_exp(code), code, desc, expected, scanned, f"+{diff}", "SOBRANTE", resolver(code)),
                None, colors, False,
            ))

    return tuple(rows)


def _metrics(model) -> Metrics:
    """Métricas del panel resumen (fiel a _update_all_ui de main.py)."""
    scanned_count = sum(
        len(p) for code, p in model.scanned_items.items() if not model.is_qr_code(code)
    )
    expected_count = sum(model.original_quantities.values())
    diff_net = scanned_count - expected_count

    unlisted = sum(
        len(p) for code, p in model.scanned_items.items()
        if not model.is_qr_code(code) and code not in model.full_family_map
    )
    excess = sum(
        max(0, len(p) - model.original_quantities.get(code, 0))
        for code, p in model.scanned_items.items()
        if not model.is_qr_code(code) and model.original_quantities.get(code, 0) > 0
    )
    relevant_diffs = unlisted + excess

    percent = (scanned_count / expected_count * 100) if expected_count > 0 else 0
    return Metrics(scanned_count, expected_count, diff_net, relevant_diffs, percent)


# ---------------------------------------------------------------------------
# Rebuild completo
# ---------------------------------------------------------------------------

def build_full_view(model, options: Optional[ViewOptions] = None) -> ScanView:
    """Proyección completa del estado visual desde el modelo (camino rebuild)."""
    options = options if options is not None else ViewOptions()
    return ScanView(
        scanned=_scanned_view(model, options),
        master=_master_view(model, options),
        diff=_diff_view(model, options),
        metrics=_metrics(model),
    )


# ---------------------------------------------------------------------------
# Aplicación incremental de un evento
# ---------------------------------------------------------------------------

def _apply_sequential(prev_rows: Tuple[RowSpec, ...], event, model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Reproyecta SOLO las filas afectadas por el evento (modo secuencial).

    Las filas afectadas son: todas las posiciones del SKU del evento (para
    mantener "última aparición con cantidad" y colores) más los QRs de los
    contenedores activos del producto (faltantes/✓).

    El orden visual es una función de la posición (p-1 en "último abajo",
    len(seq)-p en "último arriba"), por lo que cada fila reproyectada se
    reinserta en O(1) sin reordenar el resto.
    """
    sku = event["sku"]
    pos = event["pos"]
    is_qr = event.get("is_qr", model.is_qr_code(sku))

    affected_positions = set()
    affected_positions.update(model.scanned_items.get(sku, []))
    if event.get("replaced") and event.get("old_sku"):
        affected_positions.add(pos)
    if not is_qr:
        box, sec = model.get_containers_for_index(model.scan_sequence, pos - 1)
        for c in (box, sec):
            if c:
                affected_positions.update(model.scanned_items.get(c, []))

    if not affected_positions:
        return prev_rows

    seq_len = len(model.scan_sequence)
    new_specs = {}
    for p in affected_positions:
        new_specs[p] = _scanned_row_for(model, options, p - 1)

    rows = [r for r in prev_rows if _pos_from_key(r.key) not in affected_positions]
    reverse = options.sort_mode in (SORT_LAST_TOP, SORT_SCAN)

    inserts = []
    for p, spec in new_specs.items():
        if spec is None:
            continue
        idx_visual = (seq_len - p) if reverse else (p - 1)
        inserts.append((idx_visual, spec))

    for idx_visual, spec in sorted(inserts, key=lambda x: x[0]):
        rows.insert(idx_visual, spec)

    return tuple(rows)


def _apply_grouped(model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Regenera la tabla agrupada desde el modelo.

    Decisión de diseño: en modos agrupados (Alfabético/Cantidad) la fila de
    un SKU puede cambiar de posición (p. ej. al cambiar su cantidad en modo
    "Cantidad"), por lo que la proyección completa de ESTA tabla es la opción
    correcta y barata (una fila por SKU, sin QRs). El paso 3 podrá usar el
    fallback "dirty" para estos modos si hiciera falta.
    """
    return _scanned_view(model, options)


def _apply_master(prev_rows: Tuple[RowSpec, ...], event, model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Actualiza solo la fila maestra del SKU del evento."""
    sku = event["sku"]
    if event.get("is_qr", model.is_qr_code(sku)):
        return prev_rows  # los QRs no aparecen en la tabla maestra

    new_row = None
    for code, desc, qty in model.stock_data:
        if code == sku:
            scanned = len(model.scanned_items.get(sku, []))
            colors = model.get_row_color(qty, scanned)
            new_row = RowSpec(
                TABLE_MASTER, f"m:{sku}",
                (code, desc, f"{qty} ({scanned})"),
                None, colors, False,
            )
            break

    rows = [r for r in prev_rows if r.key != f"m:{sku}"]
    if new_row is not None:
        # stock_data está ordenado por código → insertar en la posición correcta
        index = sum(1 for r in rows if r.values[0] < sku)
        rows.insert(index, new_row)
    return tuple(rows)


def _apply_diff(prev_rows: Tuple[RowSpec, ...], event, model, options: ViewOptions) -> Tuple[RowSpec, ...]:
    """Actualiza solo la fila de diferencias del SKU del evento."""
    sku = event["sku"]
    if event.get("is_qr", model.is_qr_code(sku)):
        return prev_rows  # los QRs no generan diferencias

    rows = [r for r in prev_rows if r.key != f"d:{sku}"]

    scanned = len(model.scanned_items.get(sku, []))
    expected = model.original_quantities.get(sku, 0)
    desc = next((d for c, d, _e in model.stock_data if c == sku), None)
    resolver = options.location_resolver if options.location_resolver else (lambda c: DEFAULT_LOCATION)
    no_exp = EXPORT_EXCLUDED if sku in options.excluded_from_export else EXPORT_INCLUDED

    if desc is not None:
        # Código del CSV: faltante, sobrante o nada
        if scanned < expected:
            diff = expected - scanned
            colors = model.get_row_color(expected, scanned)
            new_row = RowSpec(
                TABLE_DIFF, f"d:{sku}",
                (no_exp, sku, desc, expected, scanned, f"-{diff}", "FALTANTE", resolver(sku)),
                None, colors, False,
            )
            index = _diff_insert_index(rows, True, sku, model)
            rows.insert(index, new_row)
        elif scanned > expected:
            diff = scanned - expected
            colors = model.get_row_color(expected, scanned)
            new_row = RowSpec(
                TABLE_DIFF, f"d:{sku}",
                (no_exp, sku, desc, expected, scanned, f"+{diff}", "SOBRANTE", resolver(sku)),
                None, colors, False,
            )
            index = _diff_insert_index(rows, False, sku, model)
            rows.insert(index, new_row)
    else:
        # Código desconocido: sobrante si tiene unidades escaneadas (expected 0)
        if scanned > 0:
            colors = model.get_row_color(0, scanned)
            new_row = RowSpec(
                TABLE_DIFF, f"d:{sku}",
                (no_exp, sku, "", 0, scanned, f"+{scanned}", "SOBRANTE", resolver(sku)),
                None, colors, False,
            )
            index = _diff_insert_index(rows, False, sku, model)
            rows.insert(index, new_row)

    return tuple(rows)


def _diff_insert_index(rows: Tuple[RowSpec, ...], is_faltante: bool, sku: str, model) -> int:
    """Índice de inserción de una fila de diff, fiel al orden de _diff_view:
    faltantes en orden de stock_data, luego sobrantes en orden de scanned_items.
    """
    stock_order = {c: i for i, (c, _d, _e) in enumerate(model.stock_data)}
    scanned_order = {c: i for i, c in enumerate(model.scanned_items)}

    if is_faltante:
        return sum(
            1 for r in rows
            if r.values[6] == "FALTANTE"
            and stock_order.get(r.values[1], -1) < stock_order.get(sku, -1)
        )

    n_faltantes = sum(1 for r in rows if r.values[6] == "FALTANTE")
    return n_faltantes + sum(
        1 for r in rows
        if r.values[6] == "SOBRANTE"
        and scanned_order.get(r.values[1], -1) < scanned_order.get(sku, -1)
    )


def _event_is_consistent(event, model) -> bool:
    """El evento debe corresponder a una posición real del SKU en el modelo.

    El modelo es la única fuente de verdad: si el evento no coincide con él
    (retransmisión, borrado concurrente, evento fabricado), el incremental no
    puede confiar en el evento → el llamador debe caer al rebuild.
    """
    sku = event.get("sku")
    pos = event.get("pos")
    if not sku or not pos:
        return False
    if pos < 1 or pos > len(model.scan_sequence):
        return False
    return model.scan_sequence[pos - 1] == sku


def apply_event(view: ScanView, event: dict, model, options: Optional[ViewOptions] = None):
    """Aplica un evento de escaneo (ya reflejado en el modelo) a la vista.

    Devuelve (nueva_vista, acciones). Si el evento es inconsistente con el
    modelo, cae al rebuild completo (fallback "dirty"): la vista resultante
    siempre es correcta.
    """
    options = options if options is not None else ViewOptions()

    if not _event_is_consistent(event, model):
        full = build_full_view(model, options)
        return full, diff_views(view, full)

    if options.sort_mode in SEQUENTIAL_SORTS:
        new_scanned = _apply_sequential(view.scanned, event, model, options)
    else:
        new_scanned = _apply_grouped(model, options)

    new_master = _apply_master(view.master, event, model, options)
    new_diff = _apply_diff(view.diff, event, model, options)
    # Las métricas se recalculan desde el modelo: es barato (dicts, no widgets)
    # y elimina cualquier riesgo de deriva por deltas acumulados.
    new_metrics = _metrics(model)

    new_view = ScanView(new_scanned, new_master, new_diff, new_metrics)
    return new_view, diff_views(view, new_view)


# ---------------------------------------------------------------------------
# Diferencias entre vistas → acciones de UI
# ---------------------------------------------------------------------------

def diff_views(old_view: ScanView, new_view: ScanView) -> Tuple[Action, ...]:
    """Calcula las acciones mínimas para transformar old_view en new_view.

    Las filas se identifican por su key estable. Un "move" solo se emite si
    cambia el ORDEN RELATIVO de filas que existen en ambas vistas (un
    corrimiento global por insertar al inicio NO genera moves).
    """
    actions = []

    for table in (TABLE_SCANNED, TABLE_MASTER, TABLE_DIFF):
        old_rows = getattr(old_view, table)
        new_rows = getattr(new_view, table)
        old_by_key = {r.key: r for r in old_rows}
        new_by_key = {r.key: r for r in new_rows}

        # 1. Deletes
        for key in old_by_key:
            if key not in new_by_key:
                actions.append(Action("delete", table, key))

        # 2. Updates (misma key, contenido distinto)
        for key, row in new_by_key.items():
            if key in old_by_key and old_by_key[key] != row:
                actions.append(Action("update", table, key, row=row))

        # 3. Moves (solo si cambia el orden relativo de filas comunes)
        old_common = [r.key for r in old_rows if r.key in new_by_key]
        new_common = [r.key for r in new_rows if r.key in old_by_key]
        if old_common != new_common:
            working = list(old_common)
            for target_pos, key in enumerate(new_common):
                cur = working.index(key)
                if cur != target_pos:
                    actions.append(Action("move", table, key, index=target_pos))
                    working.pop(cur)
                    working.insert(target_pos, key)

        # 4. Inserts (índice = posición final en la vista nueva)
        for key, row in new_by_key.items():
            if key not in old_by_key:
                index = next(i for i, r in enumerate(new_rows) if r.key == key)
                actions.append(Action("insert", table, key, row=row, index=index))

    return tuple(actions)


# ---------------------------------------------------------------------------
# Aplicador puro (misma semántica que tendrá el aplicador Tk del paso 3)
# ---------------------------------------------------------------------------

def apply_actions(view: ScanView, actions: Tuple[Action, ...]) -> ScanView:
    """Aplica acciones a una vista y devuelve la vista resultante.

    Orden de aplicación: deletes → updates → moves → inserts (ascendentes).
    """
    tables = {
        TABLE_SCANNED: list(view.scanned),
        TABLE_MASTER: list(view.master),
        TABLE_DIFF: list(view.diff),
    }

    def find_index(table: str, key: str) -> int:
        for i, row in enumerate(tables[table]):
            if row.key == key:
                return i
        raise KeyError(key)

    for action in actions:
        if action.op == "delete":
            tables[action.table] = [r for r in tables[action.table] if r.key != action.key]

    for action in actions:
        if action.op == "update":
            tables[action.table][find_index(action.table, action.key)] = action.row

    for action in actions:
        if action.op == "move":
            idx = find_index(action.table, action.key)
            row = tables[action.table].pop(idx)
            tables[action.table].insert(action.index, row)

    for action in sorted((a for a in actions if a.op == "insert"), key=lambda a: a.index):
        tables[action.table].insert(action.index, action.row)

    return ScanView(
        scanned=tuple(tables[TABLE_SCANNED]),
        master=tuple(tables[TABLE_MASTER]),
        diff=tuple(tables[TABLE_DIFF]),
        metrics=view.metrics,
    )
