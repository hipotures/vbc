import logging
import threading
from typing import Type, Callable, List, Dict, Any, Optional
from vbc.clients.tui.events import Event

logger = logging.getLogger(__name__)

class EventBus:
    """Client-local synchronous event bus (copy of the server one; no server imports)."""
    
    def __init__(self):
        self._subscribers: Dict[Type[Event], List[Callable[[Any], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: Type[Event], callback: Optional[Callable[[Any], None]] = None):
        """Subscribes a callback to a specific event type. Can be used as a decorator."""
        if callback is None:
            def decorator(func: Callable[[Any], None]):
                self.subscribe(event_type, func)
                return func
            return decorator

        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def publish(self, event: Event):
        """Publishes an event to all interested subscribers."""
        event_type = type(event)
        with self._lock:
            callbacks = tuple(self._subscribers.get(event_type, ()))

        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                logger.exception("EventBus subscriber failed for %s", event_type.__name__)
