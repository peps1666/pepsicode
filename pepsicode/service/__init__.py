"""Resident service mode (M3): a service process owns sessions and runs.

Clients (TUI, desktop, QQ) attach and detach freely; closing a control
connection does not cancel authorized work.  Nothing in this package starts
workers by itself — execution still goes through the shared
:mod:`pepsicode.orchestration` host and the ``execution/`` entry points.
"""
