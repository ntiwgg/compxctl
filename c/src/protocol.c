#include "compx.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>

volatile sig_atomic_t cx_signal;

void cx_error(CxError *e, CxCode code, const char *fmt, ...) {
    if (!e) return;
    e->code = code;
    va_list ap;
    va_start(ap, fmt);
    (void)vsnprintf(e->message, sizeof(e->message), fmt, ap);
    va_end(ap);
}

int64_t cx_now_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

bool cx_number(const char *s, unsigned min, unsigned max, unsigned *out) {
    if (!s || !*s || *s == '-' || *s == '+' || *s == ' ' || *s == '\t') return false;
    errno = 0;
    char *end;
    unsigned long n = strtoul(s, &end, 0);
    if (errno || *end || n < min || n > max) return false;
    *out = (unsigned)n;
    return true;
}

uint8_t cx_checksum(const uint8_t *p, size_t n) {
    unsigned sum = 0;
    for (size_t i = 0; i < n; ++i) sum += p[i];
    return (uint8_t)(0x55u - sum);
}

bool cx_valid_frame(const uint8_t *p, size_t n) {
    if (n != CX_FRAME || p[0] != 9) return false;
    return cx_checksum(p, n) == 0;
}

static bool window(unsigned addr, size_t n, CxError *e) {
    if (n == 0 || n > 10 || addr >= 65536 || n > 65536u - addr) {
        cx_error(e, CX_REFUSED, "Config window must fit 0x0000..0xffff; payload 1..10 bytes");
        return false;
    }
    return true;
}

bool cx_read_frame(unsigned addr, unsigned n, uint8_t out[CX_FRAME], CxError *e) {
    if (!window(addr, n, e)) return false;
    memset(out, 0, CX_FRAME);
    out[0] = 8; out[1] = 8; out[3] = (uint8_t)(addr >> 8);
    out[4] = (uint8_t)addr; out[5] = (uint8_t)n;
    out[16] = cx_checksum(out, 16);
    return true;
}

bool cx_write_frame(unsigned addr, const uint8_t *data, size_t n,
                    uint8_t out[CX_FRAME], CxError *e) {
    if (!data || !window(addr, n, e)) return false;
    if (!cx_read_frame(addr, (unsigned)n, out, e)) return false;
    out[1] = 7;
    memcpy(out + 6, data, n);
    out[16] = cx_checksum(out, 16);
    return true;
}

bool cx_dpi_payload(unsigned dpi, uint8_t out[4], CxError *e) {
    if (dpi < 50 || dpi > 6400 || dpi % 50) {
        cx_error(e, CX_REFUSED, "DPI must be 50..6400, in steps of 50");
        return false;
    }
    unsigned code = dpi / 50 - 1;
    out[0] = out[1] = (uint8_t)code; out[2] = 0;
    out[3] = cx_checksum(out, 3);
    return true;
}

CxSlot cx_slot(const uint8_t raw[4]) {
    CxSlot s = { .known = true };
    memcpy(s.raw, raw, 4);
    s.checksum_ok = cx_checksum(raw, 4) == 0;
    s.empty = raw[0] == 255 && raw[1] == 255 && raw[2] == 255 && raw[3] == 255;
    s.decoded = !s.empty && s.checksum_ok && raw[2] == 0 && raw[0] <= 127 && raw[1] <= 127;
    if (s.decoded) { s.dpi_x = ((unsigned)raw[0]+1)*50; s.dpi_y = ((unsigned)raw[1]+1)*50; }
    return s;
}

/* Track all global state needed to identify both report IDs and wire lengths.
 * Reject malformed descriptors instead of trusting a successfully parsed prefix. */
bool cx_descriptor(const uint8_t *p, size_t n, CxDescriptor *out, CxError *e) {
    typedef struct { unsigned id, size, count; } Global;
    Global g = {0}, stack[16];
    size_t depth = 0;
    unsigned feature[256] = {0}, input[256] = {0};
    memset(out, 0, sizeof(*out));
    for (size_t i = 0; i < n;) {
        uint8_t prefix = p[i++];
        if (prefix == 0xfe) {
            if (n-i < 2 || (size_t)p[i] > n-i-2) goto malformed;
            i += 2u + p[i];
            continue;
        }
        unsigned len = prefix & 3u;
        if (len == 3) len = 4;
        if (len > n-i) goto malformed;
        uint32_t value = 0;
        for (unsigned j=0; j<len; ++j) value |= (uint32_t)p[i+j] << (8*j);
        i += len;
        unsigned kind = (prefix >> 2) & 3u, tag = prefix >> 4;
        if (kind == 1) {
            switch (tag) {
                case 7: g.size = value; break;
                case 8: if (value == 0 || value > 255 || len != 1) goto malformed;
                        g.id = value; break;
                case 9: g.count = value; break;
                case 10: if (depth == 16 || len) goto malformed; stack[depth++] = g; break;
                case 11: if (!depth || len) goto malformed; g = stack[--depth]; break;
                default: break;
            }
        } else if (kind == 0 && (tag == 8 || tag == 11)) {
            if (g.size > 65536 || g.count > 65536 ||
                (g.count && g.size > 65536/g.count)) goto malformed;
            unsigned *bits = tag == 8 ? &input[g.id] : &feature[g.id];
            unsigned add = g.size*g.count;
            if (*bits > 65536-add) goto malformed;
            *bits += add;
        }
    }
    if (depth) goto malformed;
    out->feature8_bits = feature[8]; out->input9_bits = input[9]; out->feature6_bits = feature[6];
    out->feature8 = feature[8] == 128; out->input9 = input[9] == 128;
    out->feature6 = feature[6] == 56;
    return true;
malformed:
    cx_error(e, CX_PROTOCOL, "Malformed or oversized HID report descriptor");
    return false;
}

bool cx_drain(CxSession *s, CxError *e) {
    uint8_t report[128];
    for (unsigned i=0; i<256; ++i) {
        if (cx_signal) { cx_error(e,CX_INTERRUPTED,"Interrupted"); return false; }
        int n = s->transport.receive(s->transport.context, report, sizeof(report), 0, e);
        if (n < 0) return false;
        if (n == 0) return true;
    }
    cx_error(e, CX_PROTOCOL, "Input queue did not settle; refusing a potentially stale response");
    return false;
}

bool cx_request(CxSession *s, const uint8_t command[CX_FRAME], uint8_t reply[CX_FRAME], CxError *e) {
    if (s->silent) { cx_error(e,CX_TIMEOUT,"Device stopped answering; remaining reads skipped"); return false; }
    bool unexpected = false;
    for (unsigned attempt=0; attempt<s->tries; ++attempt) {
        if (!cx_drain(s,e) || !s->transport.send(s->transport.context,command,CX_FRAME,e)) return false;
        int64_t deadline = cx_now_ms() + s->timeout_ms;
        for (;;) {
            int64_t remaining = deadline-cx_now_ms();
            if (remaining <= 0) break;
            uint8_t in[128];
            int n = s->transport.receive(s->transport.context,in,sizeof(in),(unsigned)remaining,e);
            if (n < 0) return false;
            if (n == 0) break;
            if (!cx_valid_frame(in,(size_t)n)) { unexpected = true; continue; }
            if (in[1] == 7 || in[1] == 0x11) continue; /* device acknowledgements */
            bool match = command[1] == 4 ? in[1] == 4 : memcmp(in+1,command+1,5) == 0;
            if (!match) { unexpected = true; continue; }
            memcpy(reply,in,CX_FRAME);
            return true;
        }
    }
    if (!unexpected) s->silent = true;
    cx_error(e,unexpected ? CX_PROTOCOL : CX_TIMEOUT,
             unexpected ? "No matching valid answer; damaged, delayed or foreign frames received" :
             "No answer; mouse may be asleep or switched off");
    return false;
}
