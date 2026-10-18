"""In-memory registry populated only from trusted application definitions."""

from jarvis.tools.errors import DuplicateToolError, SealedToolRegistryError, UnknownToolError
from jarvis.tools.models import ToolDefinition


class ToolRegistry:
    """Registry managed by trusted application code.

    Registering definitions changes executable policy and is reserved for trusted
    application initialization. The dispatcher seals the registry at construction.
    This lifecycle does not prevent arbitrary trusted-process code from mutating
    private internals directly.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._sealed = False

    def register(self, definition: ToolDefinition) -> None:
        """Register a trusted definition; requests cannot call this method."""
        if self._sealed:
            raise SealedToolRegistryError()
        if definition.name in self._definitions:
            raise DuplicateToolError()
        self._definitions[definition.name] = definition

    def seal(self) -> None:
        """Prevent later registrations; safe to call more than once."""
        self._sealed = True

    @property
    def is_sealed(self) -> bool:
        return self._sealed

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError:
            raise UnknownToolError() from None
