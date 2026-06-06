"""Logging configuration for pepsicode Python.

Provides structured logging with:
- Leveled logging (DEBUG/INFO/WARNING/ERROR)
- Console and file output
- Logging at key points (API calls, tool execution, permission checks)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from pepsicode.config import PEPSI_CODE_DIR

# Path to the log file
LOG_FILE = PEPSI_CODE_DIR / "pepsicode.log"

# Log formats
CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s %(filename)s:%(lineno)d: %(message)s"


def setup_logging(
    level: str = "WARNING",
    log_to_file: bool = True,
    log_to_console: bool = True,
) -> logging.Logger:
    """Configure the pepsicode logging system.

    Args:
        level: Log level (DEBUG/INFO/WARNING/ERROR)
        log_to_file: Whether to write output to the log file
        log_to_console: Whether to write output to the console

    Returns:
        The configured root logger
    """
    # Ensure the log directory exists
    if log_to_file:
        PEPSI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    
    root_logger = logging.getLogger("pepsicode")
    root_logger.setLevel(getattr(logging, level.upper(), logging.WARNING))
    
    # configure handlers
    root_logger.handlers.clear()
    
    # File handler
    if log_to_file:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        root_logger.addHandler(file_handler)
    
    # configure handler
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(getattr(logging, level.upper(), logging.WARNING))
        console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        root_logger.addHandler(console_handler)
    
    # Reduce noise from third-party libraries
    for noisy_lib in ["urllib3", "httpx", "openai"]:
        logging.getLogger(noisy_lib).setLevel(logging.WARNING)
    
    root_logger.info("Logging initialized (level=%s, file=%s, console=%s)", level, log_to_file, log_to_console)
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a submodule.

    Args:
        name: Submodule name (e.g. 'agent_loop', 'tools.read_file')

    Returns:
        The configured child logger
    """
    return logging.getLogger(f"pepsicode.{name}")


def log_api_call(model: str, tokens_in: int, tokens_out: int, cost: float, duration_ms: float) -> None:
    """Log information about an API call."""
    logger = get_logger("api")
    logger.info(
        "API call: model=%s, tokens_in=%d, tokens_out=%d, cost=$%.4f, duration=%dms",
        model, tokens_in, tokens_out, cost, duration_ms,
    )


def log_tool_execution(tool_name: str, success: bool, duration_ms: float, error: str | None = None) -> None:
    """Log information about a tool execution."""
    logger = get_logger("tools")
    if success:
        logger.debug("Tool %s executed successfully in %dms", tool_name, duration_ms)
    else:
        logger.warning("Tool %s failed after %dms: %s", tool_name, duration_ms, error)


def log_permission_check(kind: str, target: str, granted: bool) -> None:
    """Log the result of a permission check."""
    logger = get_logger("permissions")
    if granted:
        logger.debug("Permission granted: %s for %s", kind, target)
    else:
        logger.warning("Permission denied: %s for %s", kind, target)


def log_session_event(event: str, details: str = "") -> None:
    """Log a session event (startup, save, resume)."""
    logger = get_logger("session")
    if details:
        logger.info("Session %s: %s", event, details)
    else:
        logger.info("Session %s", event)
