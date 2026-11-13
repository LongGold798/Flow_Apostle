from collections import defaultdict
from queue import Empty, Queue


class EventEngine:
    """Dispatch events on the caller's thread, including events published by handlers."""

    def __init__(self):
        self.queue = Queue(maxsize=128)
        self.handlers = defaultdict(list)

    def subscribe(self, topic, handler):
        self.handlers[topic].append(handler)

    def publish(self, topic, data):
        self.queue.put_nowait((topic, data))

    def drain(self, limit=32):
        for _ in range(limit):
            try:
                topic, data = self.queue.get_nowait()
            except Empty:
                break
            for handler in self.handlers[topic]:
                handler(data)
