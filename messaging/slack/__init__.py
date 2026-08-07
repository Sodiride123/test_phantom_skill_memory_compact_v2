"""Slack messaging adapter with a lazy public import.

Keeping ``interface`` lazy avoids importing the CLI once from this package's
initializer and then executing it a second time under ``python -m``.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from messaging.slack.interface import SlackInterface

__all__ = ["SlackInterface"]


def __getattr__(name: str):
    if name == "SlackInterface":
        from messaging.slack.interface import SlackInterface

        return SlackInterface
    raise AttributeError(name)
