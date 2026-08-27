"""
Pipeline de escaneo desacoplado — Stock Cellular Center V8.0 (Fase A)

Separa la RECEPCIÓN del escaneo (hilo principal, camino crítico mínimo)
del PROCESAMIENTO secundario (worker único, hilo aparte, sin Tkinter).

Garantías de diseño:
- FIFO estricto: el worker procesa un evento por vez, en orden de llegada.
- Sin límite artificial: queue.Queue() sin maxsize -> una ráfaga de 50+ códigos
  queda acumulada sin pérdida.
- El worker NUNCA toca Tkinter. Publica dicts en result_queue; el hilo
  principal los consume con after().
- Un error procesando un evento jamás mata al worker ni detiene la cola:
  se encola un toast de error y se continúa con el siguiente.
- Cierre ordenado mediante stop_event (threading.Event).
"""

import queue
import threading


def compute_scan_alerts(inventory, sku, pos, fam=None):
    """
    Calcula las alertas/notificaciones para un escaneo ya registrado.

    Reproduce EXACTAMENTE la lógica de avisos que antes corría en
    StockApp._on_scan_event (V8 baseline):
      1. "no pertenece al listado de stock" (aviso, no bloquea)
      2. "fuera de orden" (proximidad, con conteo agrupado de unidades)
      3. "SOBRANTE" (unidades escaneadas > esperado en CSV)

    Retorna una lista de dicts listos para result_queue:
      {"type": "toast", "mtype", "msg", "duration", "use_history", "metadata"}

    NO toca widgets. Corre en el worker. Puede ejecutarse en tests sin Tk.
    """
    alerts = []
    if not sku or inventory.is_qr_code(sku):
        return alerts

    # 1. Código no pertenece al listado (aviso; el registro ya ocurrió)
    if sku not in inventory.full_family_map:
        alerts.append({
            "type": "toast", "mtype": "error",
            "msg": f"¡ATENCIÓN! {sku} no pertenece al listado de stock.",
            "duration": 10000, "use_history": False,
        })
    else:
        # 2. Proximidad / mal guardado (agrupado por unidades, V8)
        prox_result = inventory.check_proximity(sku, pos)
        if prox_result:
            curr_c = prox_result["current_container"]
            affected_count = 0
            with inventory.lock:
                seq_snapshot = list(inventory.scan_sequence)
            for i, c in enumerate(seq_snapshot):
                if c == sku and inventory.get_containers_for_index(seq_snapshot, i)[0] == curr_c:
                    affected_count += 1
            unit_str = f" ({affected_count} unidades)" if affected_count > 1 else ""
            alerts.append({
                "type": "toast", "mtype": "error",
                "msg": (
                    f"¡Cuidado! {sku}{unit_str} fuera de orden inmediato. "
                    f"Escaneado en {curr_c} pero pertenece a {prox_result['expected_container']}"
                ),
                "duration": 10000, "use_history": True, "metadata": prox_result,
            })

        # 3. Sobrante
        with inventory.lock:
            scanned_list = list(inventory.scanned_items.get(sku, []))
        expected = inventory.original_quantities.get(sku, 0)
        if expected > 0 and len(scanned_list) > expected:
            alerts.append({
                "type": "toast", "mtype": "warning",
                "msg": f"¡SOBRANTE! {sku} tiene {len(scanned_list)} unidades (Esperado: {expected})",
                "duration": 10000, "use_history": False,
            })

    return alerts


class ScanWorker:
    """
    Worker único y secuencial (FIFO) para el procesamiento secundario de escaneos.

    - Lee de event_queue (eventos {sku, pos, fam, ts, replaced, is_qr}).
    - Calcula alertas con compute_scan_alerts (datos puros + InventoryManager).
    - Publica en result_queue: dicts "toast" y un "refresh" por evento.
    - Nunca importa ni toca Tkinter.
    """

    def __init__(self, inventory, event_queue, result_queue, stop_event):
        self.inventory = inventory
        self.event_queue = event_queue
        self.result_queue = result_queue
        self.stop_event = stop_event

    def run(self):
        while not self.stop_event.is_set():
            try:
                event = self.event_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            except Exception:
                continue

            try:
                sku = event.get("sku")
                pos = event.get("pos")
                fam = event.get("fam")

                if event.get("is_qr"):
                    # Los QRs estructurales no generan alertas, solo refresco
                    self.result_queue.put({"type": "refresh", "sku": sku})
                else:
                    alerts = compute_scan_alerts(self.inventory, sku, pos, fam)
                    for alert in alerts:
                        self.result_queue.put(alert)
                    self.result_queue.put({"type": "refresh", "sku": sku})
            except Exception as e:
                # Un error jamás mata al worker ni detiene la cola
                try:
                    self.result_queue.put({
                        "type": "toast", "mtype": "error",
                        "msg": f"Error procesando {event.get('sku', '?')}: {e}",
                        "duration": 4000, "use_history": False,
                    })
                except Exception:
                    pass
            finally:
                try:
                    self.event_queue.task_done()
                except Exception:
                    pass
