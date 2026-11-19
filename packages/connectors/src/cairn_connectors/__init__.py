"""Cairn connectors: importers that bring source content and ACLs into Cairn.

Phase 0 defines only the ``Connector`` protocol. Phase 1 adds the importers
(GitHub, chat export, meeting notes and ADR files) and the records they yield.
"""

from typing import Protocol

__all__ = ["Connector"]


class Connector(Protocol):
    """A source importer.

    Phase 0 stub: only the connector kind is fixed. Phase 1 adds the import
    methods and the record types they return.
    """

    @property
    def kind(self) -> str:
        """Stable connector kind, for example ``"github"`` or ``"chat_export"``."""
        ...
