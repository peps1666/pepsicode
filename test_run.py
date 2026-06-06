"""Quick test to verify pepsicode Python TUI functionality in mock mode."""

import os
import sys
from pathlib import Path

# Set mock mode before importing
os.environ["PEPSI_CODE_MODEL_MODE"] = "mock"

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from pepsicode.config import load_runtime_config
from pepsicode.permissions import PermissionManager
from pepsicode.prompt import build_system_prompt
from pepsicode.tools import create_default_tool_registry
from pepsicode.tty_app import run_tty_app

def main():
    cwd = str(Path.cwd())
    print("Starting pepsicode Python in mock mode...")
    print()
    
    try:
        runtime = load_runtime_config(cwd)
    except Exception as e:
        print(f"鈿狅笍  Config warning: {e}")
        runtime = None
    
    tools = create_default_tool_registry(cwd, runtime=runtime)
    permissions = PermissionManager(cwd, prompt=None)
    
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                cwd,
                permissions.get_summary(),
                {
                    "skills": tools.get_skills(),
                    "mcpServers": tools.get_mcp_servers(),
                },
            ),
        }
    ]
    
    print(f"鉁?Model: {runtime.get('model', 'mock') if runtime else 'mock'}")
    print(f"鉁?Tools: {len(tools.list())} available")
    print(f"鉁?Skills: {len(tools.get_skills())} discovered")
    print(f"鉁?MCP Servers: {len(tools.get_mcp_servers())} configured")
    print()
    print("Starting TUI... (type /exit to quit)")
    print()
    
    try:
        run_tty_app(
            runtime=runtime,
            tools=tools,
            model=None,  # Will use mock from env
            messages=messages,
            cwd=cwd,
            permissions=permissions,
        )
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        print(f"\n\n鉂?Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        tools.dispose()

if __name__ == "__main__":
    main()
