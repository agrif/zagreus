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
        return self

    def __exit__(self, type, value, traceback):
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
        reads, _, excepts = select.select(fds, [], fds)

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

    def handle_menu_key(self, c):
        c = base_key(c)
        menu = pretty_key(self.menu_key)

        helps = []
        def pressed(letters, helptext):
            helps.append((pretty_key(letters[0]), helptext))
            return c in letters.lower()

        if pressed('r', 'reset'):
            self.send_command(zagreus.server.RESET)
        elif pressed('c', 'clear screen'):
            self.console.write(CLEAR)
        elif pressed('xq', 'exit'):
            self.close()
        elif pressed(base_key(self.menu_key), 'send ' + menu):
            # repeated prefix sends prefix
            self.send(c)
        elif pressed('h?', 'help'):
            with self.console:
                self.console.write('====\n')
                for (key, desc) in helps:
                    self.console.write('{} {}\t{}\n'.format(menu, key, desc))
                self.console.write('====\n')

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
