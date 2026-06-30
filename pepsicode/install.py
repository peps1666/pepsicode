"""Interactive installer for pepsicode Python.

Configures model, API credentials, and installs launcher script.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from pepsicode.config import (
    PEPSI_CODE_DIR,
    PEPSI_CODE_SETTINGS_PATH,
    load_effective_settings,
    save_mini_code_settings,
)


def _read_input(prompt: str, default: str | None = None) -> str:
    """Read input from user with optional default value."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
        return value or default or ""
    except (EOFError, KeyboardInterrupt):
        print("\n\nInstallation cancelled.")
        sys.exit(0)


def _require_input(prompt: str, default: str | None = None) -> str:
    """Require non-empty input, with optional default."""
    while True:
        value = _read_input(prompt, default)
        if value:
            return value
        print("This field cannot be empty. Please enter a value.")


def _mask_secret(secret: str | None) -> str:
    """Show masked secret status."""
    if not secret:
        return "[not set]"
    return "[saved]"


def _install_launcher_script() -> str | None:
    """Install launcher script to platform-specific bin directory.

    Returns the installation path, or None if skipped.
    """
    home = Path.home()

    # Determine target bin directory and script based on platform
    if sys.platform == "win32":
        # Windows: Use ~/.pepsi-code/bin with .bat script
        target_bin_dir = PEPSI_CODE_DIR / "bin"
        launcher_path = target_bin_dir / "pepsicode.bat"
        python_exe = sys.executable.replace("/", "\\")
        launcher_script = "\r\n".join([
            "@echo off",
            "REM pepsicode Python Launcher for Windows",
            f'"{python_exe}" -m pepsicode.main %*',
            "",
        ])
        launcher_command = "pepsicode.bat"
    elif sys.platform == "darwin":
        # macOS: Use ~/.local/bin with bash script (also works with zsh)
        target_bin_dir = home / ".local" / "bin"
        launcher_path = target_bin_dir / "pepsicode-py"
        python_exe = sys.executable
        launcher_script = "\n".join([
            "#!/usr/bin/env bash",
            "# pepsicode Python Launcher for macOS",
            "# Works with bash, zsh, and other shells",
            "set -euo pipefail",
            f'exec "{python_exe}" -m pepsicode.main "$@"',
            "",
        ])
        launcher_command = "pepsicode-py"
    else:
        # Linux: Use ~/.local/bin with bash script
        target_bin_dir = home / ".local" / "bin"
        launcher_path = target_bin_dir / "pepsicode-py"
        python_exe = sys.executable
        launcher_script = "\n".join([
            "#!/usr/bin/env bash",
            "# pepsicode Python Launcher for Linux",
            "set -euo pipefail",
            f'exec "{python_exe}" -m pepsicode.main "$@"',
            "",
        ])
        launcher_command = "pepsicode-py"

    resolved = str(target_bin_dir.resolve())
    if '..' in str(target_bin_dir) or '~' in str(target_bin_dir):
        print("Warning: install path contains unsafe characters, skipping installation.")
        return None

    if launcher_path.exists():
        answer = _read_input(f"Launcher {launcher_path} already exists, overwrite? (y/N)", "N")
        if answer.lower() != "y":
            print("Skipped launcher installation.")
            return str(launcher_path), launcher_command, str(target_bin_dir)

    try:
        target_bin_dir.mkdir(parents=True, exist_ok=True)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(dir=str(target_bin_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(launcher_script)
            os.replace(tmp_path, str(launcher_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Make executable on Unix-like systems
        if sys.platform != "win32":
            current_permissions = launcher_path.stat().st_mode
            launcher_path.chmod(current_permissions | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        return str(launcher_path), launcher_command, str(target_bin_dir)
    except OSError as e:
        print(f"\nWarning: failed to install launcher script: {e}")
        print("You can manually create a launcher script to invoke pepsicode.")
        return None


def _check_path_entry(target_dir: str) -> bool:
    """Check if target directory is in PATH."""
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    return target_dir in path_entries


def main() -> None:
    """Run the interactive installer."""
    print("=" * 60)
    print("  pepsicode Python Installer")
    print("=" * 60)
    print()
    print(f"Configuration will be written to {PEPSI_CODE_SETTINGS_PATH}")
    print("Configuration is stored in a separate directory and will not affect other local tool configs.")
    print()

    # Load existing settings
    try:
        settings = load_effective_settings()
    except Exception:
        settings = {}

    current_env = settings.get("env", {})

    # Collect configuration
    print("Please enter configuration details:")
    print()

    model = _require_input(
        "Model name",
        settings.get("model") or current_env.get("ANTHROPIC_MODEL", ""),
    )

    base_url = _require_input(
        "ANTHROPIC_BASE_URL",
        current_env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )

    saved_auth_token = current_env.get("ANTHROPIC_AUTH_TOKEN", "")
    token_status = _mask_secret(saved_auth_token)
    token_input = _read_input(
        f"ANTHROPIC_AUTH_TOKEN {token_status}",
        None,
    )
    auth_token = token_input or saved_auth_token

    if not auth_token and not saved_auth_token:
        print("\nError: ANTHROPIC_AUTH_TOKEN cannot be empty.")
        sys.exit(1)

    auth_token = auth_token or saved_auth_token

    # Save configuration
    print("\nSaving configuration...")
    try:
        save_mini_code_settings({
            "model": model,
            "env": {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": auth_token,
                "ANTHROPIC_MODEL": model,
            },
        })
        print(f"Configuration saved to: {PEPSI_CODE_SETTINGS_PATH}")
    except OSError as e:
        print(f"\nError: failed to save configuration: {e}")
        sys.exit(1)

    # Install launcher script
    print("\nInstalling launcher...")
    launcher_result = _install_launcher_script()

    if launcher_result:
        launcher_path, launcher_command, target_bin_dir = launcher_result
        print(f"Launcher installed: {launcher_path}")

        # Check PATH and provide platform-specific instructions
        if not _check_path_entry(target_bin_dir):
            print()
            print("Warning: your PATH does not include", target_bin_dir)
            print()
            if sys.platform == "win32":
                print("Add the following path to your system PATH:")
                print(f"  {target_bin_dir}")
                print()
                print("How to add to PATH on Windows:")
                print("  1. Press Win+R and enter sysdm.cpl")
                print("  2. Advanced -> Environment Variables")
                print("  3. Find Path under User variables")
                print("  4. Add:", target_bin_dir)
            elif sys.platform == "darwin":
                print("Add the following line to ~/.zshrc (macOS defaults to zsh):")
                print(f'  export PATH="{target_bin_dir}:$PATH"')
                print()
                print("Quick add on macOS:")
                print(f'  echo \'export PATH="{target_bin_dir}:$PATH"\' >> ~/.zshrc')
                print("  source ~/.zshrc")
            else:
                print("Add the following line to ~/.bashrc or ~/.zshrc:")
                print(f'  export PATH="{target_bin_dir}:$PATH"')
                print()
                print("Quick add on Linux (bash):")
                print(f'  echo \'export PATH="{target_bin_dir}:$PATH"\' >> ~/.bashrc')
                print("  source ~/.bashrc")
        else:
            print()
            print(f"You can now launch by typing `{launcher_command}` in any terminal.")

    # Final summary
    print()
    print("=" * 60)
    print("  Installation complete!")
    print("=" * 60)
    print()
    print("Configuration file:", PEPSI_CODE_SETTINGS_PATH)
    if launcher_result:
        launcher_path, launcher_command, _ = launcher_result
        print("Launch command:", launcher_command)
    print()
    print("How to launch on each platform:")
    print()
    print("  Windows:")
    print("    pepsicode.bat               (if added to PATH)")
    print("    python -m pepsicode.main    (generic method)")
    print()
    print("  macOS:")
    print("    pepsicode-py                (if added to PATH)")
    print("    python3 -m pepsicode.main   (generic method)")
    print()
    print("  Linux:")
    print("    pepsicode-py                (if added to PATH)")
    print("    python3 -m pepsicode.main   (generic method)")
    print()
    print("Thanks for using pepsicode Python!")
    print()


if __name__ == "__main__":
    main()
