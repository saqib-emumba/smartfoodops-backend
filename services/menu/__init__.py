"""SmartFoodOps Menu Service — published menus and the cache-aside read (Port 8003).

A package rather than loose modules so its own modules import absolutely
(`from menu.repository import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/menu/.
"""
