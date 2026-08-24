"""SmartFoodOps Rider Service — the delivery fleet and proximity dispatch (Port 8006).

A package rather than loose modules so its own modules import absolutely
(`from rider.repository import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/rider/.
"""
