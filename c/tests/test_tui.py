def main():
    """Real pseudo-terminal: navigation, confirmed write, readback, cancellation,
    resize, and terminal restoration on normal quit / SIGTERM / SIGINT."""
    import errno
    import fcntl
    import os
    import pty
    import select
    import signal
    import struct
    import subprocess
    import sys
    import termios
    import time

    binary = os.path.abspath(sys.argv[1])

    def start():
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
        before = termios.tcgetattr(slave)
        p = subprocess.Popen([binary, 'tui', '--demo'], stdin=slave, stdout=slave, stderr=slave)
        return p, master, slave, before

    def until(master, needle, timeout=5):
        data = b''
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as e:
                    if e.errno == errno.EIO: break
                    raise
                data += chunk
                if needle in data: return data
        raise AssertionError((needle, data[-4000:]))

    p, master, slave, before = start()
    try:
        until(master, b'DEMO - NO HARDWARE')
        os.write(master, b'2')
        until(master, b'e edits')
        os.write(master, b'j')
        until(master, b'editable')
        os.write(master, b'e')
        until(master, b'Esc cancels')
        os.write(master, b'1600\r')
        until(master, b'y confirms')
        os.write(master, b'y')
        until(master, b'Slot 1: 1600 DPI')
        os.write(master, b'e')
        until(master, b'Esc cancels')
        os.write(master, b'2400\r')
        until(master, b'y confirms')
        os.write(master, b'n')
        until(master, b'Cancelled; no changes sent')
        os.write(master, b'3')
        until(master, b'HOST PARAMETER')
        os.write(master, b'4p')
        until(master, b'Read-only dump complete')
        os.write(master, b'6')
        until(master, b'RGB, button remapping')
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 10, 40, 0, 0))
        until(master, b'at least 80 columns')
        os.write(master, b'q')
        assert p.wait(timeout=5) == 0
        assert termios.tcgetattr(slave) == before
    finally:
        if p.poll() is None: p.kill(); p.wait()
        os.close(master); os.close(slave)

    for sig in (signal.SIGTERM, signal.SIGINT):
        p, master, slave, before = start()
        try:
            until(master, b'DEMO - NO HARDWARE')
            p.send_signal(sig)
            assert p.wait(timeout=5) == 128+sig
            assert termios.tcgetattr(slave) == before
            until(master, b'\x1b[?1049l')
        finally:
            if p.poll() is None: p.kill(); p.wait()
            os.close(master); os.close(slave)
    print('TUI: confirmed edit, cancellation, navigation, dump, resize, SIGINT/SIGTERM restoration OK')


if __name__ == '__main__':
    main()
