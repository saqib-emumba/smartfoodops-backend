"""SmartFoodOps Restaurant Service — onboarding and lookups (Port 8002).

A package rather than loose modules so its own modules import absolutely
(`from restaurant.repositories.restaurants import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/restaurant/.
"""
