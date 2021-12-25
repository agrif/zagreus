import logging
import os
import re
import select
import socket
import time

import click
import daemonize as daemonize_mod
import RPi.GPIO as GPIO
import serial

APP_NAME = 'zagreus'
PID_FILE = '/tmp/zagreus.pid'
SOCK_FILE = '/tmp/zagreus.sock'

logger = logging.getLogger(APP_NAME)

ESCAPE = b'\xff'
RESET = b'r'
ESCAPE_RE = re.compile(b'(' + re.escape(ESCAPE) + b'.)', re.DOTALL)

def encode(text):
    return text.encode('utf-8', 'replace').replace(ESCAPE, ESCAPE + ESCAPE)

def decode(data):
    for chunk in ESCAPE_RE.split(data):
        if chunk.startswith(ESCAPE):
            yield (True, chunk[len(ESCAPE):])
        else:
            yield (False, chunk.decode('utf-8', 'replace'))

def command(c):
    return ESCAPE + c

class Z80:
    def __init__(self, reset_pin, port, baud, encoding='ascii'):
        self.port = serial.Serial(port, baud, timeout=0)
        self.reset_pin = reset_pin
        self.encoding = encoding

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(reset_pin, GPIO.OUT)

    def close(self):
        self.port.close()

    def __exit__(self, type, value, traceback):
        self.close()

    def __enter__(self):
        return self

    def fileno(self):
        return self.port.fileno()

    def read(self, amount=1):
        return self.port.read(amount).decode(self.encoding, 'replace')

    def write(self, data):
        # FIXME slow the writes
        self.port.write(data.encode(self.encoding, 'replace'))

    def reset(self):
        GPIO.output(self.reset_pin, 1)
        time.sleep(0.1) # FIXME hmmm
        GPIO.output(self.reset_pin, 0)

class Z80Server:
    def __init__(self, sock, z80):
        self.sock = sock
        self.connections = []
        self.conn_addrs = {}
        self.z80 = z80
        self.z80.reset()
        self.buffer_size = 1024
        self.backbuffer = ''
        self.backbuffer_max = 1024 * 8

    @classmethod
    def inet(cls, host, port, z80):
        logger.info('listening on {}:{}'.format(host, port))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen()
        return cls(s, z80)

    @classmethod
    def unix(cls, path, z80):
        if os.path.exists(path):
            os.remove(path)

        logger.info('listening on {}'.format(path))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(path)
        s.listen()
        return cls(s, z80)

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        conns = self.connections[:]
        for conn in conns:
            self.close_client(conn)

    def close_client(self, s):
        logger.info('disconnect: {}'.format(self.conn_addrs[s]))
        self.connections.remove(s)
        del self.conn_addrs[s]
        s.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def send_to_all(self, data):
        self.backbuffer += data
        if len(self.backbuffer) > self.backbuffer_max:
            self.backbuffer = self.backbuffer[-self.backbuffer_max:]
        broken = []
        for conn in self.connections:
            try:
                conn.sendall(encode(data))
            except Exception:
                broken.append(conn)
        for b in broken:
            self.close_client(b)

    def serve_once(self, timeout=None):
        if self.sock is None:
            raise RuntimeError('server has been closed')

        fds = [self.sock, self.z80] + self.connections
        reads, _, excepts = select.select(fds, [], fds, timeout)

        for s in excepts:
            if s is self.sock:
                # the server socket has died, close down
                self.close()
            elif s is self.z80:
                # the z80 has died?? ????
                self.close()
            else:
                # a client connection has died, close it down
                self.close_client(s)

        for s in reads:
            if s is self.sock:
                # accept a new connection
                conn, addr = s.accept()
                if isinstance(addr, tuple):
                    # inet socket
                    addr = addr[0]
                else:
                    # unix socket
                    addr = 'local'
                logger.info('connect: {}'.format(addr))
                self.connections.append(conn)
                self.conn_addrs[conn] = addr
                conn.sendall(encode(self.backbuffer))
            elif s is self.z80:
                # the z80 has some data
                data = s.read(self.buffer_size)
                logger.debug('[z80]: {}'.format(repr(data)))
                self.send_to_all(data)
            elif s in self.connections:
                # a connection has incoming data
                try:
                    data = s.recv(self.buffer_size)
                except Exception:
                    data = b''
                if data:
                    for (is_cmd, chunk) in decode(data):
                        if is_cmd:
                            logger.debug('[{}]: command {}'.format(self.conn_addrs[s], repr(chunk)))
                            self.handle_command(chunk)
                        else:
                            logger.debug('[{}]: {}'.format(self.conn_addrs[s], repr(chunk)))
                            # turn ENTER into CR+LF
                            chunk = chunk.replace('\n', '\r\n')
                            self.z80.write(chunk)
                else:
                    self.close_client(s)

    def serve_forever(self):
        while self.sock is not None:
            self.serve_once()

    def serve_until_idle(self, timeout=10.0):
        last_connection_at = time.time()
        while self.sock is not None:
            self.serve_once(timeout=timeout)
            now = time.time()
            if self.connections:
                last_connection_at = now
            if last_connection_at + timeout < now:
                self.close()
                logger.info('server idle, exiting.')
                break
            

    def handle_command(self, c):
        if c == RESET:
            self.z80.reset()
            self.send_to_all('\n')

@click.command()
@click.option('-h', '--host', default='localhost',
              help='hostname to bind to')
@click.option('-p', '--port', default=9999, type=int,
              help='port number to bind to')
@click.option('-u', '--unix-socket', default=None,
              help='unix socket to bind to')
@click.option('-x', '--exit-when-idle', is_flag=True,
              help='exit when last client disconnects')
@click.option('-d', '--daemonize', is_flag=True,
              help='daemonize after start')
@click.option('--pid-file', default=PID_FILE,
              help='path to pid file')
@click.option('-r', '--reset-pin', default=4, type=int,
              help='pin number connected to z80 reset')
@click.option('-s', '--serial-port', default='/dev/ttyS0',
              help='serial port connected to z80')
@click.option('-b', '--baud', default=115200, type=int,
              help='baud rate to use for z80 serial port')
@click.option('--debug', is_flag=True,
              help='enable debug logging')
def main(host, port, unix_socket, exit_when_idle, daemonize, pid_file,
         reset_pin, serial_port, baud, debug):

    logging.basicConfig()

    logger.setLevel(level=logging.INFO)
    if debug:
        logger.setLevel(logging.DEBUG)

    if unix_socket:
        unix_socket = os.path.abspath(unix_socket)
    if pid_file:
        pid_file = os.path.abspath(pid_file)
    if serial_port:
        serial_port = os.path.abspath(serial_port)

    def action(*args):
        z = Z80(reset_pin, serial_port, baud)
        if unix_socket:
            server = Z80Server.unix(unix_socket, z)
        else:
            server = Z80Server.inet(host, port, z)

        with server:
            if exit_when_idle:
                server.serve_until_idle()
            else:
                server.serve_forever()

    daemon = daemonize_mod.Daemonize(app=APP_NAME, pid=pid_file, action=action, foreground=(not daemonize), logger=None if daemonize else logger, verbose=debug)
    daemon.start()

def start_in_background(reset_pin, serial_port, baud):
    # fork, because daemonize start() will call exit()
    # so this had better be in a new process
    if os.fork() != 0:
        # parent
        return

    unix_socket = SOCK_FILE
    pid_file = PID_FILE
    if serial_port:
        serial_port = os.path.abspath(serial_port)

    def action(*args):
        z = Z80(reset_pin, serial_port, baud)
        server = Z80Server.unix(unix_socket, z)

        with server:
            server.serve_until_idle()

    daemon = daemonize_mod.Daemonize(app=APP_NAME, pid=pid_file, action=action)
    daemon.start()

if __name__ == '__main__':
    main()
