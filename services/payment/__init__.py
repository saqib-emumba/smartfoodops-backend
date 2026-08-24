"""SmartFoodOps Payment Service — authorisation and refunds (Port 8005).

A package rather than loose modules so its own modules import absolutely
(`from payment.repository import ...`) and cannot be shadowed by a top-level
dependency of the same name. The Dockerfile copies it to /app/payment/.
"""
