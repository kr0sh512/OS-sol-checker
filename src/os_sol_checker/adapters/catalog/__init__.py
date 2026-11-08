"""Safe Git catalogs, immutable test data and sanitized task metadata."""
from .git import GitCatalog
from .tree import CatalogError

__all__ = ["CatalogError", "GitCatalog"]
