# Informe A — extracción de `Dashboard`

## Cambios realizados

- `Dashboard.__init__` guarda como atributos el estado que antes vivía en `run_curses_tui` y los valores de la vista que necesitan compartir los métodos. `_configure_curses` conserva la configuración de curses; `run` inicia el scanner y coordina el ciclo principal.
- `cached_context`, `trigger_led_sync`, `_led_work`, `start_async_compaction`, `_compact_worker`, `trigger_quota_poll`, `_quota_worker` y `db_wal_sig` separan las cachés y tareas de fondo. Sus trabajadores siguen usando los mismos módulos con enlace tardío (`_sessions`, `_compact`, `_accounts`, `_tmuxctl`, `_paths`).
- `execute_action`, `_execute_global_action` y `_execute_session_action` separan el despacho de acciones, cambios de vista y acciones de sesión.
- `_scanner`, `_start_scanner`, `_process_compact_results`, `_adopt_scan_snapshot`, `_periodic_idle_check` y `_advance_background` separan escaneo, resultados de compactación y mantenimiento periódico.
- `_prepare_frame`, `_draw_header`, `_draw_quota_alert` y `_compute_layout` preparan los datos y el encabezado.
- `_draw_session_list`, `_draw_buttons`, `_draw_inspector`, `_draw_preview`, `_draw_compaction_modal` y `_draw_live_preview` separan la lista, los botones, el inspector y las dos variantes de vista previa.
- `_draw_accounts`, `_draw_account_removal`, `_draw_account_details`, `_draw_vault`, `_draw_memory` y `_draw_footer` dibujan las demás vistas y el pie. `draw` conserva su orden de invocación.
- `handle_mouse` y `handle_key` procesan entrada. `_handle_account_key`, `_handle_vault_key`, `_handle_pending_compaction_key` y `_handle_session_key` conservan las ramas de teclado por vista.
- `run_curses_tui(stdscr)` mantiene su firma y delega en `Dashboard(stdscr).run()`.
- `test_tui.py` añade pruebas con un `stdscr` mock para la delegación, el estado inicial, `q`, `r` y las flechas.

No se cambiaron mensajes, comandos, orden de dibujo ni valores de retorno de las acciones. Todos los métodos de `Dashboard` tienen como máximo 150 líneas.

## Bugs observados y no corregidos

- `agy_dash/tui.py:426`: la validación de API key combina `not entered.startswith("nvapi-")` y `len(entered) < 20` con `and`; una cadena larga sin prefijo puede pasar la validación.
- `agy_dash/tui.py:363`: el acceso a `self.sessions[self.saved_session_cursor]` no comprueba el límite cuando la vista no es `sessions`; un cursor guardado obsoleto podría producir `IndexError`.

## Verificaciones solicitadas

Ejecutadas desde `tools/agy-dash/`.

Comando:

```text
python3 -m pytest -q test_agy_dashboard.py test_tui.py
```

Salida real completa:

```text
Pytest: 24 passed, 1 failed in 0.25s

Failures:
1. [FAIL] AgyDashboardTests.test_zig_scan_returns_valid_structure
     test_agy_dashboard.py:19: in test_zig_scan_returns_valid_structure
     self.assertTrue(ad.ensure_zig_core(), "agy-fast-core must build/exist")
     E   AssertionError: False is not true : agy-fast-core must build/exist
```

Comando:

```text
ruff check --select F agy_dash test_tui.py
```

Salida real completa:

```text
Ruff: No issues found
```

## Pendiente

La suite no queda completamente verde: la prueba existente `test_zig_scan_returns_valid_structure` falla porque `ensure_zig_core()` devuelve `False` y no hay un binario `agy-fast-core` disponible. No se modificó ningún archivo fuera de los autorizados para intentar corregirlo. La extracción y las dos pruebas nuevas sí están hechas; el requisito de que pasen todas las pruebas queda pendiente por ese fallo.
