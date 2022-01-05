import atexit
import curses
import fcntl
import re
import select
import signal
import socket
import sys
import termios
import time

import click

import zagreus.expect
import zagreus.server

# https://www.windmill.co.uk/ascii-control-codes.html
TABLE_LOWER = r'2abcdefghijklmnopqrstuvwxyz[\]6-'
TABLE_UPPER = r'@ABCDEFGHIJKLMNOPQRSTUVWXYZ{|}^_'
TABLE_NAMES = r'@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\]^_'

def control(c):
    try:
        return chr(TABLE_LOWER.index(c))
    except ValueError:
        pass
    try:
        return chr(TABLE_UPPER.index(c))
    except ValueError:
        pass
    raise ValueError('control code not found: ^{}'.format(s))

def base_key(c):
    if ord(c) < len(TABLE_LOWER):
        return TABLE_LOWER[ord(c)]
    return c.lower()

def pretty_key(c):
    if ord(c) < len(TABLE_NAMES):
        return 'C-' + TABLE_NAMES[ord(c)]
    return c.upper()

# https://code.activestate.com/recipes/475116-using-terminfo-for-portable-color-output-cursor-co/
curses.setupterm()
DELAY_RE = re.compile(r'\$<\d+>[/*]?')
def tigetstr(cap):
    s = curses.tigetstr(cap).decode('utf-8') or ''
    return DELAY_RE.sub('', s)

CLEAR = tigetstr('clear')

# mostly taken from
# https://github.com/pyserial/pyserial/blob/master/serial/tools/miniterm.py
class Console:
    def __init__(self, client):
        self.client = client
        self.input = sys.stdin
        self.output = sys.stdout
        self.fd = self.input.fileno()
        self.old = termios.tcgetattr(self.fd)
        atexit.register(self.cleanup)
        signal.signal(signal.SIGINT, self.sigint)

        self.setup()

    # use context manager to temporarily enter normal mode
    def __enter__(self):
        self.cleanup()
        self.write('====\n')
        return self

    def __exit__(self, type, value, traceback):
        self.write('====\n')
        self.setup()

    def fileno(self):
        return self.fd

    def setup(self):
        new = termios.tcgetattr(self.fd)
        new[3] = new[3] & ~termios.ICANON & ~termios.ECHO & ~termios.ISIG
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, new)

    def cleanup(self):
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)
        self.write('\n')

    def sigint(self, sig, frame):
        self.client.running = False
        self.cancel()

    def getkey(self):
        c = self.input.read(1)
        if c == chr(0x7f):
            c = chr(8) # map BS (which yields DEL) to backspace
        return c

    def cancel(self):
        fcntl.ioctl(self.fd, termios.TIOCSTI, b'\0')

    def write(self, text):
        self.output.write(text)
        self.output.flush()

class Z80Client:
    def __init__(self, sock):
        self.sock = sock
        self.running = True
        self.console = Console(self)
        self.in_menu = False
        self.expect = None

        self.buffer_size = 1024
        self.menu_key = control('a') # C-t

    @classmethod
    def inet(cls, host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        return cls(s)

    @classmethod
    def unix(cls, path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        return cls(s)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.console.cancel()
            self.console.cleanup()
            self.sock = None
            self.running = False

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def send(self, text):
        self.sock.sendall(zagreus.server.encode(text))

    def send_command(self, c):
        self.sock.sendall(zagreus.server.command(c))

    def run(self):
        while self.sock is not None:
            self.run_once()

    def run_once(self):
        if not self.running:
            self.close()
            return

        fds = [self.sock, self.console]
        timeout = None
        if self.expect is not None:
            timeout = self.expect.timeout
        reads, _, excepts = select.select(fds, [], fds, timeout)

        if excepts:
            # something has gone wrong!
            self.close()
            return

        for fd in reads:
            if fd is self.sock:
                # server has new info for us
                data = fd.recv(self.buffer_size)
                for (is_cmd, chunk) in zagreus.server.decode(data):
                    if is_cmd:
                        pass
                    else:
                        # fix up some odd control characters
                        chunk = chunk.replace('\f', CLEAR)
                        self.console.write(chunk)
                        if self.expect is not None:
                            self.expect.interact(chunk)
                            
            elif fd is self.console:
                # console has new key for us
                c = self.console.getkey()
                if self.in_menu:
                    self.handle_menu_key(c)
                    self.in_menu = False
                elif c == self.menu_key:
                    self.in_menu = True
                else:
                    self.send(c)

        # make sure to jog the script at least once
        if self.expect is not None:
            self.expect.interact()

    def run_script(self, expect):
        self.expect = expect
        self.expect.on_output = self.send
        self.expect.on_error = self.handle_script_error
        self.expect.interact()

    def handle_script_error(self, type, value, traceback):
        with self.console:
            self.console.write('error in script `{}`\n'.format(self.expect.name))
            self.console.write('{}: {}\n'.format(type.__name__, value))

    @zagreus.expect.Expect.script
    def small_computer_monitor(self):
        self.send_command(zagreus.server.RESET)
        yield from zagreus.expect.expect('Small Computer Monitor - RC2014\r\n*', timeout=3)

    @zagreus.expect.Expect.script
    def cpm(self):
        yield from self.small_computer_monitor()
        yield from zagreus.expect.sleep(0.3)
        yield from zagreus.expect.send('CPM\n')
        yield from zagreus.expect.expect('A>', timeout=5)

    @zagreus.expect.Expect.script
    def basic(self):
        yield from self.small_computer_monitor()
        yield from zagreus.expect.send('BASIC\n')
        yield from zagreus.expect.expect('Memory top? ')
        yield from zagreus.expect.send('\n')
        yield from zagreus.expect.expect('Ok')

    def handle_menu_key(self, c):
        c = base_key(c)
        menu = pretty_key(self.menu_key)

        helps = []
        def pressed(letters, helptext):
            helps.append((pretty_key(letters[0]), helptext))
            return c in letters.lower()

        if pressed('r', 'reset to small computer monitor'):
            self.run_script(self.small_computer_monitor())
        elif pressed('l', 'clear screen'):
            self.console.write(CLEAR)
        elif pressed('xq', 'exit'):
            self.close()
        elif pressed('c', 'boot CP/M'):
            self.run_script(self.cpm())
        elif pressed('b', 'boot BASIC'):
            self.run_script(self.basic())
        elif pressed(base_key(self.menu_key), 'send ' + menu):
            # repeated prefix sends prefix
            self.send(c)
        elif pressed('h?', 'help'):
            with self.console:
                for (key, desc) in helps:
                    self.console.write('{} {}\t{}\n'.format(menu, key, desc))

@click.command()
@click.option('-h', '--host', default=None,
              help='hostname to connect to')
@click.option('-p', '--port', default=9999, type=int,
              help='port number to connect to')
@click.option('-u', '--unix-socket', default=None,
              help='unix socket to connect to')
@click.option('-r', '--reset-pin', default=4, type=int,
              help='pin number connected to z80 reset')
@click.option('-s', '--serial-port', default='/dev/ttyS0',
              help='serial port connected to z80')
@click.option('-b', '--baud', default=115200, type=int,
              help='baud rate to use for z80 serial port')
def main(host, port, unix_socket, reset_pin, serial_port, baud):
    if unix_socket:
        client = Z80Client.unix(unix_socket)
    elif host:
        client = Z80Client.inet(host, port)
    else:
        client = None
        try:
            client = Z80Client.unix(zagreus.server.SOCK_FILE)
        except Exception:
            pass
        if not client:
            zagreus.server.start_in_background(reset_pin, serial_port, baud)
            for _ in range(5):
                try:
                    client = Z80Client.unix(zagreus.server.SOCK_FILE)
                    break
                except Exception:
                    time.sleep(1.0)
            else:
                raise RuntimeError('could not start background server')
    with client:
        client.run()

if __name__ == '__main__':
    main()
