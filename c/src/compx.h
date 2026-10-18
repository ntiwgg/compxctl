#ifndef COMPX_H
#define COMPX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <signal.h>

#define CX_VERSION "5.0.0-dev"
#define CX_FRAME 17
#define CX_MAX_DEVICES 32
#define CX_PATH 4096
#define CX_SLOTS 8
#define CX_SCHEMA 3

typedef enum { CX_OK, CX_IO, CX_TIMEOUT, CX_PROTOCOL, CX_REFUSED, CX_INTERRUPTED } CxCode;
typedef struct { CxCode code; char message[384]; } CxError;
typedef struct {
    char id[128], name[128], node[CX_PATH], usb_dir[CX_PATH];
    unsigned product;
    double advertised_ms;
    bool advertised_known;
} CxDevice;
typedef struct {
    bool feature8, input9, feature6;
    unsigned feature8_bits, input9_bits, feature6_bits;
} CxDescriptor;
typedef struct {
    uint8_t raw[4];
    bool known, checksum_ok, empty, decoded;
    unsigned dpi_x, dpi_y;
} CxSlot;
typedef struct {
    CxDevice device;
    bool host_known; unsigned host_raw;
    bool stored_known; unsigned stored_code;
    bool active_known; unsigned active_slot;
    CxSlot slots[CX_SLOTS];
    bool battery_known, charging; unsigned battery, battery_state;
    char warnings[8][384]; size_t warning_count;
} CxSnapshot;
typedef struct {
    bool demo, json;
    char device_id[128];
    unsigned timeout_ms, tries;
} CxOptions;

/* Test seam below the protocol: the real reader and fake device share the same
 * drain/request implementation, including zero-time polls and absolute deadlines. */
typedef struct {
    void *context;
    int (*receive)(void *, uint8_t *, size_t, unsigned, CxError *);
    bool (*send)(void *, const uint8_t *, size_t, CxError *);
} CxTransport;
typedef struct {
    CxTransport transport;
    int fd, lock_fd;
    unsigned timeout_ms, tries;
    bool silent, demo;
    CxDevice device;
} CxSession;
typedef struct {
    bool changed, write_attempted, readback_verified;
    char description[256];
} CxMutation;

extern volatile sig_atomic_t cx_signal;
void cx_error(CxError *, CxCode, const char *, ...);
int64_t cx_now_ms(void);
bool cx_number(const char *, unsigned, unsigned, unsigned *);
bool cx_descriptor(const uint8_t *, size_t, CxDescriptor *, CxError *);
uint8_t cx_checksum(const uint8_t *, size_t);
bool cx_valid_frame(const uint8_t *, size_t);
bool cx_read_frame(unsigned, unsigned, uint8_t[CX_FRAME], CxError *);
bool cx_write_frame(unsigned, const uint8_t *, size_t, uint8_t[CX_FRAME], CxError *);
CxSlot cx_slot(const uint8_t[4]);
bool cx_dpi_payload(unsigned, uint8_t[4], CxError *);
bool cx_drain(CxSession *, CxError *);
int cx_receive_fd(void *, uint8_t *, size_t, unsigned, CxError *);
bool cx_request(CxSession *, const uint8_t[CX_FRAME], uint8_t[CX_FRAME], CxError *);
bool cx_discover(CxDevice *, size_t, size_t *, CxError *);
bool cx_choose(const CxOptions *, CxDevice *, CxError *);
bool cx_open(CxSession *, const CxDevice *, const CxOptions *, CxError *);
void cx_close(CxSession *);
bool cx_read(CxSession *, unsigned, unsigned, uint8_t *, CxError *);
bool cx_battery(CxSession *, unsigned *, bool *, unsigned *, CxError *);
bool cx_snapshot(const CxOptions *, CxSnapshot *, CxError *);
bool cx_set_dpi(CxSession *, unsigned, unsigned, CxMutation *, CxError *);
bool cx_set_interval(CxSession *, unsigned, CxMutation *, CxError *);
bool cx_live_interval(CxSession *, unsigned, CxMutation *, CxError *);
bool cx_host_rate(unsigned, bool, CxMutation *, CxError *);
bool cx_reset(const CxDevice *, bool, CxMutation *, CxError *);
void cx_json_string(const char *);
void cx_print_snapshot(const CxSnapshot *, bool);
void cx_print_mutation(const char *, const CxMutation *, const CxError *, bool);
void cx_print_capabilities(bool);
int cx_tui(const CxOptions *);
void cx_demo_reset(void);

#endif
