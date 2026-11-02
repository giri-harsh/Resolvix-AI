from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from resolvix_contracts.brain_protocol import Brain
from .config import RuntimeSettings
from .mock_brain import MockBrain

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BRAIN_INSTANCE: Brain | None = None


def get_brain(settings: RuntimeSettings | None = None) -> Brain:
    """Return the configured Brain instance (singleton per process)."""
    global _BRAIN_INSTANCE
    if _BRAIN_INSTANCE is not None:
        return _BRAIN_INSTANCE

    if settings is None:
        settings = RuntimeSettings.from_env()

    if settings.brain_impl == "real":
        try:
            from resolvix_brain import BrainSettings, create_brain  # type: ignore[import-not-found]
        except ImportError as e:
            logger.error(
                "BRAIN_IMPL is set to 'real', but resolvix_brain cannot be imported: %s",
                e,
            )
            raise RuntimeError(
                f"BRAIN_IMPL is set to 'real', but resolvix_brain cannot be imported: {e}. "
                "Ensure brain is installed ('pip install -e brain') or PYTHONPATH includes brain."
            ) from e

        try:
            logger.info("Initializing real resolvix_brain...")
            _BRAIN_INSTANCE = create_brain(BrainSettings.from_env())
        except Exception as e:
            logger.error("BRAIN_IMPL is set to 'real', but failed to initialize resolvix_brain: %s", e)
            raise RuntimeError(
                f"BRAIN_IMPL is set to 'real', but failed to initialize resolvix_brain: {e}"
            ) from e
    else:
        logger.info("Initializing MockBrain from fixture streams.")
        _BRAIN_INSTANCE = MockBrain()

    return _BRAIN_INSTANCE


def reset_brain() -> None:
    """Reset the singleton instance (primarily used for unit testing)."""
    global _BRAIN_INSTANCE
    _BRAIN_INSTANCE = None
