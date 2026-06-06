#!/usr/bin/env python3
"""
Simulate TUI input handling to verify Chinese input works correctly.
This tests the core input parsing without needing an actual terminal.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from pepsicode.tui.input_parser import parse_input_chunk
from pepsicode.tui.input import render_input_prompt
from pepsicode.tui.chrome import string_display_width

def test_chinese_input():
    """Test complete Chinese input flow."""
    print("馃И Testing Chinese Input Flow")
    print("=" * 60)
    
    # Simulate typing "浣犲ソ"
    chinese_text = "浣犲ソ"
    
    # Test 1: Parse input
    print(f"\n1. Parsing '{chinese_text}':")
    result = parse_input_chunk(chinese_text)
    
    events = result.events
    text_events = [e for e in events if hasattr(e, 'text')]
    
    print(f"   Total events: {len(events)}")
    print(f"   Text events: {len(text_events)}")
    
    for i, event in enumerate(text_events):
        char = event.text
        width = string_display_width(char)
        print(f"   Event {i+1}: '{char}' (width: {width})")
    
    # Test 2: Render with cursor
    print(f"\n2. Rendering with cursor:")
    for pos in range(len(chinese_text) + 1):
        rendered = render_input_prompt(chinese_text, pos)
        lines = rendered.split('\n')
        input_line = lines[2] if len(lines) > 2 else ""
        
        # Show cursor position
        cursor_marker = "^"
        print(f"   Position {pos}: {input_line}")
    
    # Test 3: Verify no character splitting
    print(f"\n3. Character integrity:")
    expected_chars = ['你', '好']
    actual_chars = [e.text for e in text_events]
    
    if expected_chars == actual_chars:
        print(f"   鉁?Characters NOT split: {actual_chars}")
    else:
        print(f"   鉁?MISMATCH: expected {expected_chars}, got {actual_chars}")
    
    return expected_chars == actual_chars

def test_mixed_input():
    """Test mixed Chinese and English input."""
    print(f"\n馃И Testing Mixed Input")
    print("=" * 60)
    
    test_cases = [
        "Hello 涓栫晫",
        "娴嬭瘯123",
        "Python涓枃",
    ]
    
    all_passed = True
    for text in test_cases:
        result = parse_input_chunk(text)
        text_events = [e for e in result.events if hasattr(e, 'text')]
        reconstructed = ''.join(e.text for e in text_events)
        
        passed = reconstructed == text
        all_passed = all_passed and passed
        
        status = "PASS" if passed else "FAIL"
        print(f"   {status} '{text}' -> {reconstructed}")
    
    return all_passed

if __name__ == "__main__":
    test1_passed = test_chinese_input()
    test2_passed = test_mixed_input()
    
    print("\n" + "=" * 60)
    if test1_passed and test2_passed:
        print("鉁?All tests PASSED! Chinese input works correctly.")
        print("\n瀛﹂暱鍙互鍦?Linux 涓婃祴璇?")
        print("  cd ~/code/pepsicode")
        print("  git pull")
        print("  pepsicode-py")
    else:
        print("鉂?Some tests FAILED!")
    print("=" * 60)
