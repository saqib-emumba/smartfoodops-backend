"""The event contract every producer and consumer in the platform shares (Week 3, D40).

Imported on demand only — `common/__init__.py` has no re-exports, and neither does this
package — so pulling in `common.events` costs nothing to a service that never touches
Kafka. `common/requirements.txt`'s rule ("a dependency belongs there only when a module in
common/ imports it unconditionally") is why the transport lives in `common/kafka.py`
instead of here: this package is pydantic models and stdlib only, already an unconditional
dependency everywhere.
"""
