class InvalidJobTransitionError(ValueError):
    """Raised when a job attempts an invalid lifecycle transition."""


class NotFoundError(Exception):
    def __init__(self, resource: str, id: str) -> None:
        self.resource = resource
        self.id = id
        super().__init__(f"{resource} not found: {id}")


class ConflictError(Exception):
    pass


class DomainValidationError(Exception):
    pass
