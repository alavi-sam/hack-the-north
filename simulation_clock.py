"""Shared simulation time, advanced by the server independently of network I/O."""
import time


class SimulationClock:
    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


clock = SimulationClock()
