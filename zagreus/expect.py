import functools
import sys
import time

class Command:
    def __init__(self, output='', wants_input=False, expires=None):
        self.output = output
        self.wants_input = wants_input
        self.expires = expires

    def expired(self):
        if self.expires is None:
            return False
        return time.time() > self.expires

class TimeoutError(Exception):
    pass

class Expect:
    def __init__(self, name, runner):
        self.name = name
        self.runner = runner
        self.step = None

    def on_output(self, v):
        pass

    def on_error(self, type, value, traceback):
        raise v

    def __iter__(self):
        return self.runner

    def start(self):
        try:
            self.step = next(self.runner)
        except StopIteration:
            self.step = None

    @classmethod
    def script(cls, f):
        @functools.wraps(f)
        def inner(*args, **kwargs):
            runner = f(*args, **kwargs)
            name = f.__name__
            try:
                return cls(name, iter(runner))
            except TypeError:
                def gen():
                    if False:
                        yield
                    return runner
                return cls(name, gen())
        return inner

    @property
    def timeout(self):
        if self.step is None:
            return None
        timeout = self.step.expires - time.time()
        if timeout > 0:
            return timeout
        return 0

    @property
    def running(self):
        return self.runner is not None

    def interact(self, s=None):
        if self.runner is not None and self.step is None:
            try:
                self.start()
            except Exception as e:
                self.on_error(*sys.exc_info())
                self.runner = None
                self.step = None
                return
        if self.step is None:
            return
        if self.step.output:
            self.on_output(self.step.output)
            self.step.output = ''
        while True:
            if self.step.wants_input and s is None:
                if not self.step.expired():
                    break
            try:
                self.step = self.runner.send(s)
            except StopIteration:
                self.runner = None
                self.step = None
                return
            except Exception as e:
                self.on_error(*sys.exc_info())
                self.runner = None
                self.step = None
                return
            self.on_output(self.step.output)
            self.step.output = ''
            if self.step.wants_input:
                s = None

@Expect.script
def send(s):
    yield Command(output=s)

@Expect.script
def receive(timeout=1, expires=None):
    if expires is None and timeout is not None:
        expires = time.time() + timeout
    return (yield Command(wants_input=True, expires=expires))

@Expect.script
def expect(s, timeout=1, expires=None):
    if expires is None and timeout is not None:
        expires = time.time() + timeout
    gather = ''

    while True:
        part = yield from receive(expires=expires)
        if part is None:
            raise TimeoutError('expected {!r}'.format(s))
        gather += part
        if s in gather:
            return gather

@Expect.script
def sleep(duration):
    expires = time.time() + duration
    while True:
        s = yield from receive(expires=expires)
        if s is None:
            break

if __name__ == '__main__':
    e = test()
    print(repr(e.interact(None)))
    while e.running:
        print(repr(e.interact('sent')))
