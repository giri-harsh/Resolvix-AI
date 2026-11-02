from __future__ import annotations

import sys
from unittest.mock import patch, MagicMock
import pytest

from resolvix_runtime.brain_loader import get_brain, reset_brain
from resolvix_runtime.config import RuntimeSettings
from resolvix_runtime.mock_brain import MockBrain


def test_brain_loader_mock_default():
    reset_brain()
    settings = RuntimeSettings(brain_impl="mock")
    brain = get_brain(settings)
    assert isinstance(brain, MockBrain)
    reset_brain()


def test_brain_loader_real_import_error_fails_loudly():
    reset_brain()
    settings = RuntimeSettings(brain_impl="real")
    with patch.dict(sys.modules, {"resolvix_brain": None}):
        with pytest.raises(RuntimeError) as exc_info:
            get_brain(settings)
        assert "BRAIN_IMPL is set to 'real', but resolvix_brain cannot be imported" in str(exc_info.value)
    reset_brain()


def test_brain_loader_real_init_error_fails_loudly():
    reset_brain()
    settings = RuntimeSettings(brain_impl="real")
    mock_module = MagicMock()
    mock_module.create_brain.side_effect = ValueError("Invalid Groq API key configuration")
    with patch.dict(sys.modules, {"resolvix_brain": mock_module}):
        with pytest.raises(RuntimeError) as exc_info:
            get_brain(settings)
        assert "failed to initialize resolvix_brain: Invalid Groq API key configuration" in str(exc_info.value)
    reset_brain()
