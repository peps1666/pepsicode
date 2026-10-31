"""Packaged model instructions, independent of the user's working directory."""

from importlib.resources import files

MAX_HARNESS_INSTRUCTION_BYTES = 32 * 1024


def load_harness_instructions() -> str:
    """Load the required Harness policy from package data, including frozen builds.

    Missing, invalid, empty or oversized policy is an installation error; callers
    must not silently fall back to workspace instructions or an empty prompt.
    """
    try:
        resource = files(__package__).joinpath("AGENTS.md")
        with resource.open("rb") as stream:
            data = stream.read(MAX_HARNESS_INSTRUCTION_BYTES + 1)
        if len(data) > MAX_HARNESS_INSTRUCTION_BYTES:
            raise ValueError("instruction file exceeds 32 KiB")
        content = data.decode("utf-8-sig").strip()
        if not content:
            raise ValueError("instruction file is empty")
        return content
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError(
            f"Cannot load bundled PepsiCode Harness instructions (prompts/AGENTS.md): {error}"
        ) from error
