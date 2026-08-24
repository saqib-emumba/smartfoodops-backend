"""SmartFoodOps User Service — the platform's identity provider (Port 8001).

A package rather than loose modules so its own modules import absolutely
(`from user.repository import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/user/.
"""
