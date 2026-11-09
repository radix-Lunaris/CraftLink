"""
MC-Link client package.

Version metadata lives in mclink.config.settings so importing this package
never has import side effects:

    from mclink.config import settings
    settings.APP_VERSION
"""

__all__ = ["config", "core", "gui"]
