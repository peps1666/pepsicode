from pepsicode.tui.input_parser import KeyEvent, TerminalInputDecoder, TextEvent, WheelEvent


def test_fragmented_sgr_wheel_preserves_subsequent_input():
    for split in range(1, len("\x1b[<64;23;9M")):
        decoder = TerminalInputDecoder()
        sequence = "\x1b[<64;23;9M"
        events = decoder.feed(sequence[:split]) + decoder.feed(sequence[split:] + "x")
        assert events == [WheelEvent(direction="up"), TextEvent(text="x", ctrl=False, meta=False)]


def test_unknown_csi_sequences_do_not_leak_into_draft():
    decoder = TerminalInputDecoder()
    assert decoder.feed("\x1b[?25") == []
    assert decoder.feed("h\x1b[1;2z\x1b[<66;3;4Mhello") == [
        TextEvent(text=char, ctrl=False, meta=False) for char in "hello"
    ]


def test_arrow_keys_and_plain_text_keep_their_meaning():
    decoder = TerminalInputDecoder()
    assert decoder.feed("a\x1b[A\x1b[Bb") == [
        TextEvent(text="a", ctrl=False, meta=False),
        KeyEvent(name="up", ctrl=False, meta=False),
        KeyEvent(name="down", ctrl=False, meta=False),
        TextEvent(text="b", ctrl=False, meta=False),
    ]


def test_copy_shortcut_sequences_decode_cleanly():
    decoder = TerminalInputDecoder()
    assert decoder.feed("\x1b[67;6u") == [KeyEvent(name="c", ctrl=True, meta=False, shift=True)]
    assert decoder.feed("\x1b[99;6u") == [KeyEvent(name="c", ctrl=True, meta=False, shift=True)]
    assert decoder.feed("\x1b[27;6;67~") == [KeyEvent(name="c", ctrl=True, meta=False, shift=True)]
    assert decoder.feed("\x1b[2;5~") == [KeyEvent(name="insert", ctrl=True, meta=False)]
    assert decoder.feed("\x1bc") == [TextEvent(text="c", ctrl=False, meta=True)]
