class MinerError(Exception):
    """Base exception for an expected, user-facing pipeline failure."""


class ConfigurationError(MinerError):
    """The supplied configuration is missing or invalid."""


class RunError(MinerError):
    """A run cannot be created, loaded, or transitioned as requested."""


class CollectionError(MinerError):
    """GitHub collection could not be completed safely."""


class TransientCollectionError(CollectionError):
    """A request failed in a way that may succeed with a smaller batch or later retry."""


class DataValidationError(MinerError):
    """Collected or transformed data violates an expected invariant."""
