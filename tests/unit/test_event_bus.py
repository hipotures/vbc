import pytest
from vbc.infrastructure.event_bus import EventBus
from vbc.domain.events import Event

class MockEvent(Event):
    message: str

def test_event_bus_subscribe_publish():
    bus = EventBus()
    received_events = []
    
    def callback(event: MockEvent):
        received_events.append(event)
    
    bus.subscribe(MockEvent, callback)
    
    event = MockEvent(message="hello")
    bus.publish(event)
    
    assert len(received_events) == 1
    assert received_events[0].message == "hello"

def test_event_bus_multiple_subscribers():
    bus = EventBus()
    results = {"a": False, "b": False}
    
    bus.subscribe(MockEvent, lambda e: results.update({"a": True}))
    bus.subscribe(MockEvent, lambda e: results.update({"b": True}))
    
    bus.publish(MockEvent(message="test"))
    
    assert results["a"] is True
    assert results["b"] is True

def test_event_bus_decorator_subscribe():
    bus = EventBus()
    received = []
    
    @bus.subscribe(MockEvent)
    def on_event(event: MockEvent):
        received.append(event)
        
    bus.publish(MockEvent(message="decorator"))
    assert len(received) == 1
    assert received[0].message == "decorator"
