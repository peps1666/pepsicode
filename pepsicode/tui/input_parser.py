from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Literal, Union

# Pre-compiled regexes for escape sequence parsing
_SGR_MOUSE_RE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI_CURSOR_RE = re.compile(r"^\x1b\[(?:1;(\d+))?([A-DF-H])")
_CSI_TILDE_RE = re.compile(r"^\x1b\[(\d+)~")
_CSI_OTHER_RE = re.compile(r"^\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
_SS3_RE = re.compile(r"^\x1bO([A-DF-H])")
_ESC_CHAR_RE = re.compile(r"^\x1b([^\x1b\[O])")

ParsedKeyName = Literal[
    "return", "tab", "backspace", "delete", "up", "down", "left", "right", "pageup", "pagedown", "home", "end", "escape"
]


@dataclass(frozen=True)
class KeyEvent:
    name: str  # ParsedKeyName or 'a', 'c', etc. for ctrl-keys
    ctrl: bool
    meta: bool
    kind: str = "key"


@dataclass(frozen=True)
class TextEvent:
    text: str
    ctrl: bool
    meta: bool
    kind: str = "text"


@dataclass(frozen=True)
class PasteEvent:
    text: str
    kind: str = "paste"


@dataclass(frozen=True)
class WheelEvent:
    direction: Literal["up", "down"]
    kind: str = "wheel"


@dataclass(frozen=True)
class MouseEvent:
    """Left-button press/drag/release at a 0-based screen cell."""

    action: Literal["press", "drag", "release"]
    x: int
    y: int
    kind: str = "mouse"


ParsedInputEvent = Union[KeyEvent, TextEvent, PasteEvent, WheelEvent, MouseEvent]


def _decode_mouse_button(button: int, x: int, y: int, released: bool) -> ParsedInputEvent | None:
    # Wheel events (button & 0x43 == 0x40 → up, 0x41 → down)
    if (button & 0x43) == 0x40:
        return WheelEvent(direction="up")
    if (button & 0x43) == 0x41:
        return WheelEvent(direction="down")
    if button & 0x40:
        return None
    low = button & 0x03
    if released:
        return MouseEvent(action="release", x=x, y=y)
    if low != 0:
        # Middle/right buttons, or motion without a held button.
        return None
    return MouseEvent(action="drag" if button & 0x20 else "press", x=x, y=y)


@dataclass(frozen=True)
class ParseResult:
    events: list[ParsedInputEvent]
    rest: str


class TerminalInputDecoder:
    """Keep partial UTF-8 characters and escape/paste sequences between reads."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._remainder = ""

    def feed(self, chunk: str | bytes) -> list[ParsedInputEvent]:
        text = self._decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        result = parse_input_chunk(self._remainder + text)
        self._remainder = result.rest
        return result.events


CTRL_CHAR_TO_NAME: dict[str, str] = {
    "\x01": "a",
    "\x03": "c",
    "\x05": "e",
    "\x0e": "n",
    "\x0f": "o",
    "\x10": "p",
    "\x15": "u",
}


def maybe_need_more_for_escape_sequence(chunk: str) -> bool:
    if not chunk:
        return False
    if chunk[0] != "\x1b":
        return False
    if len(chunk) == 1:
        return True

    # CSI
    if chunk[1] == "[":
        # Legacy Mouse: ESC[M...
        if len(chunk) >= 3 and chunk[2] == "M":
            return len(chunk) < 6
        # CSI parameter bytes (0x30-0x3f) precede intermediate bytes
        # (0x20-0x2f); a final byte in 0x40-0x7e completes the sequence.
        intermediate = False
        for c in chunk[2:]:
            if "\x40" <= c <= "\x7e":
                return False
            if "\x20" <= c <= "\x2f":
                intermediate = True
            elif not ("\x30" <= c <= "\x3f" and not intermediate):
                return False
        return True

    # SS3
    if chunk[1] == "O":
        return len(chunk) < 3

    # ESC + char (Alt+char)
    # We already checked len(chunk) == 1. For Alt+char, 2 chars is complete.
    return False


def parse_escape_sequence(chunk: str) -> tuple[ParsedInputEvent | None, int]:
    if not chunk or chunk[0] != "\x1b":
        return None, 0

    if len(chunk) == 1:
        return KeyEvent(name="escape", ctrl=False, meta=False), 1

    # SGR Mouse: ESC[<button;x;yM/m
    sgr_match = _SGR_MOUSE_RE.match(chunk)
    if sgr_match:
        button, x, y = (int(sgr_match.group(n)) for n in (1, 2, 3))
        released = sgr_match.group(4) == "m"
        return _decode_mouse_button(button, x - 1, y - 1, released), sgr_match.end()

    # Legacy mouse: ESC[M + three bytes offset by 32; button 3 means release.
    if chunk.startswith("\x1b[M") and len(chunk) >= 6:
        button = ord(chunk[3]) - 32
        x, y = ord(chunk[4]) - 33, ord(chunk[5]) - 33
        released = (button & 0x43) == 0x03
        return _decode_mouse_button(button, x, y, released), 6

    # CSI cursor: ESC[{1;modifier}A/B/C/D/H/F
    csi_cursor_match = _CSI_CURSOR_RE.match(chunk)
    if csi_cursor_match:
        mod_str = csi_cursor_match.group(1)
        key_char = csi_cursor_match.group(2)
        mod = int(mod_str) if mod_str else 1

        # Modifier logic: 2=Shift, 3=Alt, 4=Shift+Alt, 5=Ctrl, 6=Shift+Ctrl, 7=Alt+Ctrl, 8=Shift+Alt+Ctrl
        # 1=None.
        # (mod - 1) & 4 -> Ctrl
        # (mod - 1) & 2 -> Alt/Meta
        ctrl = bool((mod - 1) & 4)
        meta = bool((mod - 1) & 2)

        name_map: dict[str, str] = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
        return KeyEvent(name=name_map[key_char], ctrl=ctrl, meta=meta), csi_cursor_match.end()

    # CSI tilde: ESC[N~
    csi_tilde_match = _CSI_TILDE_RE.match(chunk)
    if csi_tilde_match:
        n = int(csi_tilde_match.group(1))
        # 1=home,3=delete,4=end,5=pageup,6=pagedown,7=home,8=end
        tilde_map: dict[int, str] = {1: "home", 3: "delete", 4: "end", 5: "pageup", 6: "pagedown", 7: "home", 8: "end"}
        if n in tilde_map:
            return KeyEvent(name=tilde_map[n], ctrl=False, meta=False), csi_tilde_match.end()
        return None, csi_tilde_match.end()

    # Unknown complete CSI is a terminal control sequence, not user text.
    # Consume it whole so its parameters cannot leak into the input draft.
    csi_other_match = _CSI_OTHER_RE.match(chunk)
    if csi_other_match:
        return None, csi_other_match.end()

    # SS3: ESC O A/B/C/D/H/F
    ss3_match = _SS3_RE.match(chunk)
    if ss3_match:
        key_char = ss3_match.group(1)
        name_map: dict[str, str] = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
        return KeyEvent(name=name_map[key_char], ctrl=False, meta=False), ss3_match.end()

    # Terminals commonly encode Alt+Enter as ESC followed by CR or LF.
    # Decode it before Alt+char so it cannot become text in the draft.
    if chunk.startswith(("\x1b\r", "\x1b\n")):
        return KeyEvent(name="return", ctrl=False, meta=True), 3 if chunk.startswith("\x1b\r\n") else 2

    # ESC+Tab
    if chunk.startswith("\x1b\t"):
        return KeyEvent(name="tab", ctrl=False, meta=True), 2

    # ESC+char (Alt+char)
    esc_char_match = _ESC_CHAR_RE.match(chunk)
    if esc_char_match:
        char = esc_char_match.group(1)
        return TextEvent(text=char, ctrl=False, meta=True), 2

    # Default to bare escape if nothing else matches and we are not waiting for more
    return KeyEvent(name="escape", ctrl=False, meta=False), 1


def parse_input_chunk(chunk: str) -> ParseResult:
    events: list[ParsedInputEvent] = []
    i = 0
    while i < len(chunk):
        if chunk.startswith("\x1b[200~", i):
            end = chunk.find("\x1b[201~", i + 6)
            if end < 0:
                break  # Keep the entire paste until its closing marker arrives.
            text = chunk[i + 6 : end].replace("\r\n", "\n").replace("\r", "\n")
            events.append(PasteEvent(text=text))
            i = end + 6
            continue

        # Windows consoles can deliver an unbracketed multiline paste as one read.
        # Preserve it as text rather than treating its newlines as submissions.
        if i == 0 and "\x1b" not in chunk and any(c in chunk for c in "\r\n"):
            text = chunk.rstrip("\r\n")
            if "\r" in text or "\n" in text:
                events.append(PasteEvent(text=chunk.replace("\r\n", "\n").replace("\r", "\n")))
                i = len(chunk)
                continue

        if maybe_need_more_for_escape_sequence(chunk[i:]):
            break

        char = chunk[i]

        # Escape sequence
        if char == "\x1b":
            event, consumed = parse_escape_sequence(chunk[i:])
            if event:
                events.append(event)
            i += consumed
            continue

        # CR, LF, CR+LF -> return
        if char == "\r":
            if i + 1 < len(chunk) and chunk[i + 1] == "\n":
                i += 2
            else:
                i += 1
            events.append(KeyEvent(name="return", ctrl=False, meta=False))
            continue

        if char == "\n":
            events.append(KeyEvent(name="return", ctrl=False, meta=False))
            i += 1
            continue

        # Tab
        if char == "\t":
            events.append(KeyEvent(name="tab", ctrl=False, meta=False))
            i += 1
            continue

        # Backspace (0x7f, 0x08)
        if char in ("\x7f", "\x08"):
            events.append(KeyEvent(name="backspace", ctrl=False, meta=False))
            i += 1
            continue

        # Ctrl chars (0x01-0x1a)
        if "\x01" <= char <= "\x1a":
            if char in CTRL_CHAR_TO_NAME:
                events.append(KeyEvent(name=CTRL_CHAR_TO_NAME[char], ctrl=True, meta=False))
            # Swallow other control characters
            i += 1
            continue

        # Regular text
        events.append(TextEvent(text=char, ctrl=False, meta=False))
        i += 1

    return ParseResult(events=events, rest=chunk[i:])
