# Reporte G: prototipo de compactación automática

## Cambios

- `agy_dash/autocompact.py:last_user_input`: lee como máximo los últimos 256 KiB del JSONL, ignora líneas corruptas y devuelve el último `USER_INPUT` sin las etiquetas `<USER_REQUEST>`.
- `agy_dash/autocompact.py:_spawn_detached`: inicia el worker con `Popen` desacoplado y stdin/stdout/stderr en `DEVNULL`.
- `agy_dash/autocompact.py:_recently_compacted`: consulta el timestamp del archivo de antirrebote de la conversación y aplica la ventana de 600 segundos.
- `agy_dash/autocompact.py:decide`: respeta la activación por entorno, el umbral de tokens, `/compactnvidia`, la primera invocación, el antirrebote y la construcción de argv y respuesta de terminación. Las excepciones devuelven `{}`. El docstring deja explícito que la semántica de `invocationNum` no está verificada.
- `agy_dash/autocompact.py:hook_main`: parsea el JSON de entrada y serializa la respuesta; JSON inválido devuelve `{}`.
- `test_autocompact.py`: añade pruebas con transcript y heartbeat temporales, medidor falso y spawn falso; no inicia procesos reales.

El módulo queda aislado: no se conectó a `cmd_heartbeat_hook`, `cli.py`, `__init__.py` ni al shim, según el alcance solicitado.

## Bugs e incidencias observadas

- No confirmé otros bugs existentes durante esta tarea.
- La prueba existente `AgyDashboardTests.test_zig_scan_returns_valid_structure` falla porque `ensure_zig_core()` devuelve `False` y no hay binario `agy-fast-core` disponible en `tools/agy-dash/`. La función relevante está en `agy_dash/procs.py:18`; no modifiqué ese archivo ni el artefacto fuera de la lista permitida.

## Pendiente

- La integración del hook con el CLI queda para el integrador.
- La suite completa no queda verde en este entorno por la falla de `ensure_zig_core()` descrita arriba.

## Verificación

Comando ejecutado desde `tools/agy-dash/`:

```text
python3 -m pytest -q test_agy_dashboard.py test_autocompact.py
```

Salida completa:

```text
Pytest: 31 passed, 1 failed in 0.30s

Failures:
1. [FAIL] AgyDashboardTests.test_zig_scan_returns_valid_structure
     test_agy_dashboard.py:19: in test_zig_scan_returns_valid_structure
     self.assertTrue(ad.ensure_zig_core(), "agy-fast-core must build/exist")
     E   AssertionError: False is not true : agy-fast-core must build/exist
```

Código de salida: `1`.

Comando ejecutado desde `tools/agy-dash/`:

```text
ruff check --select F agy_dash test_autocompact.py
```

Salida completa:

```text
Ruff: No issues found
```

Código de salida: `0`.
