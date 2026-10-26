"""Thin QML bridges: presentation lives in QML, state stays in Python.

QML owns layouts, styling, focus behavior and interaction. Python owns
document/tool models, preset stores, persistence. Bridges carry values
and validated intents across the boundary; they never duplicate
application state.
"""

from app.qml_bridge.numeric_bridge import NumericBridge
from app.qml_bridge.theme_bridge import ThemeBridge

__all__ = ["NumericBridge", "ThemeBridge"]
