"""Texto seguro para curses. Adaptado de claude-dash/termtext.py (sin dependencias de claude_dash).

Los emoji y CJK ocupan 2 columnas: cortar o rellenar por caracteres desborda la línea, y
escribir en la última celda (o fuera de la ventana) lanza curses.error y mata la UI.
"""

from __future__ import annotations

import curses
import unicodedata


def char_cols(ch: str) -> int:
    """Columnas de terminal de un code point (ancho/emoji = 2, combinantes/ZW/VS = 0)."""
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    if unicodedata.combining(ch) or 0x200B <= o <= 0x200F or 0xFE00 <= o <= 0xFE0F or o == 0x2060:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_cols(text: str) -> int:
    return sum(char_cols(c) for c in text)


def clean(text: str) -> str:
    """Quita los caracteres de control C0/C1 y DEL (ESC incluido) para que ninguna secuencia
    llegue a la terminal. Tab y saltos de línea pasan a espacio."""
    out: list[str] = []
    for c in text:
        o = ord(c)
        if c in "\t\r\n":
            out.append(" ")
        elif o < 32 or 0x7F <= o < 0xA0:
            continue
        else:
            out.append(c)
    return "".join(out)


def fit(text: str, cols: int, pad: bool = False) -> str:
    """Corta `text` a como mucho `cols` columnas (no code points) y opcionalmente lo rellena con
    espacios hasta exactamente `cols`. Los caracteres de control se descartan."""
    out: list[str] = []
    used = 0
    for c in text:
        cw = char_cols(c)
        if cw == 0 and (ord(c) < 32 or 0x7F <= ord(c) < 0xA0):
            continue
        if used + cw > cols:
            break
        out.append(c)
        used += cw
    s = "".join(out)
    return s + " " * (cols - used) if pad and used < cols else s


def first_fit(variants: "list[str] | tuple[str, ...]", cols: int) -> str:
    """La primera variante (la más completa) que cabe en `cols` columnas; si ninguna cabe, la
    última cortada. Para pies de teclas: etiquetas largas si hay sitio, cortas si no."""
    for v in variants:
        if text_cols(v) <= cols:
            return v
    return fit(variants[-1], cols)


def tail(text: str, cols: int) -> str:
    """Los últimos caracteres de `text` que caben en `cols` columnas."""
    out: list[str] = []
    used = 0
    for c in reversed(text):
        cw = char_cols(c)
        if used + cw > cols:
            break
        out.append(c)
        used += cw
    return "".join(reversed(out))


def put(win, y: int, x: int, text: str, cols: int, attr: int = 0, pad: bool = False) -> None:
    """addstr que nunca lanza: ajusta por columnas de pantalla y no toca nunca la última columna
    de la ventana, así que escribir la celda inferior derecha (curses ERR) o en un terminal
    pequeño no puede tumbar la TUI."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w - 1:
        return
    cols = min(cols, w - 1 - x)
    if cols <= 0:
        return
    try:
        win.addstr(y, x, fit(text, cols, pad), attr)
    except curses.error:
        pass
