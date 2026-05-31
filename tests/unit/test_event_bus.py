import pytest
import logging
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

def test_event_bus_isolates_handler_exceptions(caplog):
    bus = EventBus()
    received = []

    def broken_handler(event: MockEvent):
        raise RuntimeError("handler failed")

    def working_handler(event: MockEvent):
        received.append(event.message)

    bus.subscribe(MockEvent, broken_handler)
    bus.subscribe(MockEvent, working_handler)

    with caplog.at_level(logging.ERROR, logger="vbc.infrastructure.event_bus"):
        bus.publish(MockEvent(message="still-delivered"))

    assert received == ["still-delivered"]
    assert "EventBus subscriber failed for MockEvent" in caplog.text

def test_event_bus_uses_subscriber_snapshot_during_publish():
    bus = EventBus()
    received = []

    def late_handler(event: MockEvent):
        received.append(("late", event.message))

    def first_handler(event: MockEvent):
        received.append(("first", event.message))
        bus.subscribe(MockEvent, late_handler)

    bus.subscribe(MockEvent, first_handler)

    bus.publish(MockEvent(message="first-publish"))
    assert received == [("first", "first-publish")]

    bus.publish(MockEvent(message="second-publish"))
    assert received == [
        ("first", "first-publish"),
        ("first", "second-publish"),
        ("late", "second-publish"),
    ]
