CC ?= cc
CPPFLAGS += -D_POSIX_C_SOURCE=200809L -Isrc
CFLAGS ?= -O2 -g
WARN = -std=c17 -Wall -Wextra -Wpedantic -Werror -Wconversion -Wshadow -Wformat=2
CORE = src/protocol.c src/linux.c src/device.c src/output.c
SOURCES = $(CORE) src/tui.c src/main.c
PREFIX ?= /usr/local
DESTDIR ?=

.PHONY: all test sanitize clean install uninstall
all: build/compxctl

build:
	mkdir -p build

build/compxctl: $(SOURCES) src/compx.h | build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $(SOURCES) $(LDFLAGS) -o $@

build/test_core: tests/test_core.c $(CORE) src/compx.h | build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) tests/test_core.c $(CORE) $(LDFLAGS) -o $@

test: build/compxctl build/test_core
	./build/test_core
	python3 tests/test_cli.py ./build/compxctl
	python3 tests/test_tui.py ./build/compxctl

sanitize:
	$(MAKE) clean
	$(MAKE) test CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie' LDFLAGS='-fsanitize=address,undefined -no-pie'

install: build/compxctl
	install -Dm755 build/compxctl "$(DESTDIR)$(PREFIX)/bin/compxctl"
	install -Dm644 compxctl.1 "$(DESTDIR)$(PREFIX)/share/man/man1/compxctl.1"

uninstall:
	rm -f "$(DESTDIR)$(PREFIX)/bin/compxctl" "$(DESTDIR)$(PREFIX)/share/man/man1/compxctl.1"

clean:
	rm -rf build
