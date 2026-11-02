"""Pixel geometry traced from the supplied PepsiCode identity.

Shared by the terminal mark and the optional asset export script. No image
libraries are required at runtime. Each cell is one square logo pixel.
"""

INK = "#12141A"
LIME = "#C8F560"
PAPER = "#F4F6F8"
PIXELS = (
    "              ",
    "              ",
    "              ",
    "              ",
    "              ",
    "  ##########  ",
    " ############ ",
    "##          ##",
    "## #        ##",
    "##  #       ##",
    "## #   ___  ##",
    "##          ##",
    "##          ##",
    "##          ##",
    " ############ ",
    "  ##########  ",
    "   ##    ##   ",
    "   ##    ##   ",
)
BUBBLES = ((8.6, 1.4, 0.42), (10.5, 3.0, 0.85), (12.6, 1.1, 0.60))


def terminal_mark() -> list[str]:
    """Small terminal variant: ten columns and five rows including bubbles."""
    # Hand-fit the prompt and feet at terminal resolution rather than dropping
    # source pixels, which would erase the one-pixel details during scaling.
    pixels = (
        " ######## ",
        "#        #",
        "#  #     #",
        "#   # _  #",
        "#  #     #",
        "#        #",
        " ######## ",
        "  ##  ##  ",
    )
    grid = [[cell != " " for cell in row] for row in pixels]
    blocks = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    return [
        "     ° o °",
        *("".join(blocks[top, bottom] for top, bottom in zip(grid[y], grid[y + 1])) for y in range(0, len(grid), 2)),
    ]
