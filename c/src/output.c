#include "compx.h"
#include <stdio.h>

void cx_json_string(const char *s) {
    putchar('"');
    for (const unsigned char *p=(const unsigned char *)s;*p;++p) {
        switch (*p) {
            case '"': fputs("\\\"",stdout); break;
            case '\\': fputs("\\\\",stdout); break;
            case '\n': fputs("\\n",stdout); break;
            case '\r': fputs("\\r",stdout); break;
            case '\t': fputs("\\t",stdout); break;
            default:
                if (*p<32 || *p==127) printf("\\u%04x",*p);
                else if (*p<128) putchar(*p);
                else {
                    /* Preserve valid UTF-8, replace malformed bytes. Never emit
                     * invalid JSON or reinterpret UTF-8 bytes as Latin-1. */
                    unsigned length=(*p>=0xc2 && *p<=0xdf) ? 2 : (*p>=0xe0 && *p<=0xef) ? 3 : (*p>=0xf0 && *p<=0xf4) ? 4 : 0;
                    bool valid=length!=0;
                    for (unsigned i=1;i<length;++i) {
                        if (!p[i] || (p[i]&0xc0)!=0x80) { valid=false; break; }
                    }
                    if (valid && ((*p==0xe0 && p[1]<0xa0) || (*p==0xed && p[1]>=0xa0) ||
                                  (*p==0xf0 && p[1]<0x90) || (*p==0xf4 && p[1]>=0x90))) valid=false;
                    if (valid) { for (unsigned i=0;i<length;++i) putchar(p[i]); p+=length-1; }
                    else fputs("\\ufffd",stdout);
                }
        }
    }
    putchar('"');
}

static void nullable_unsigned(bool known,unsigned value) {
    if (known) printf("%u",value); else fputs("null",stdout);
}

void cx_print_snapshot(const CxSnapshot *s,bool json) {
    if (json) {
        printf("{\"schema\":%d,\"command\":\"status\",\"device\":{\"id\":",CX_SCHEMA);
        cx_json_string(s->device.id); fputs(",\"name\":",stdout); cx_json_string(s->device.name);
        fputs(",\"node\":",stdout); cx_json_string(s->device.node);
        printf(",\"product\":\"%04x\"},\"polling\":{\"advertised_interval_ms\":",s->device.product);
        if (s->device.advertised_known) printf("%.6f",s->device.advertised_ms); else fputs("null",stdout);
        fputs(",\"host_parameter_raw\":",stdout); nullable_unsigned(s->host_known,s->host_raw);
        fputs(",\"measured_rate_hz\":null,\"application_verified\":false},\"stored_interval_code\":",stdout);
        nullable_unsigned(s->stored_known,s->stored_code);
        fputs(",\"active_slot\":",stdout); nullable_unsigned(s->active_known,s->active_slot);
        fputs(",\"dpi_slots\":[",stdout);
        for (unsigned i=0;i<CX_SLOTS;++i) {
            const CxSlot *p=&s->slots[i];
            printf("%s{\"slot\":%u,\"known\":%s,\"checksum_ok\":",i ? "," : "",i,p->known ? "true" : "false");
            if (p->known) fputs(p->checksum_ok ? "true" : "false",stdout); else fputs("null",stdout);
            fputs(",\"dpi_x\":",stdout); nullable_unsigned(p->decoded,p->dpi_x);
            fputs(",\"dpi_y\":",stdout); nullable_unsigned(p->decoded,p->dpi_y);
            fputs(",\"bytes\":",stdout);
            if (p->known) printf("\"%02x %02x %02x %02x\"",p->raw[0],p->raw[1],p->raw[2],p->raw[3]);
            else fputs("null",stdout);
            putchar('}');
        }
        fputs("],\"battery\":",stdout);
        if (s->battery_known) printf("{\"percent\":%u,\"charging\":%s,\"state\":%u}",s->battery,s->charging ? "true" : "false",s->battery_state);
        else fputs("null",stdout);
        fputs(",\"warnings\":[",stdout);
        for (size_t i=0;i<s->warning_count;++i) { if (i) putchar(','); cx_json_string(s->warnings[i]); }
        puts("]}"); return;
    }
    printf("%s  [25a7:%04x]  USB %s\nConfig interface: %s\n",s->device.name,s->device.product,s->device.id,
           s->device.node[0] ? s->device.node : "unavailable");
    if (s->device.advertised_known) printf("Advertised interval: %.3f ms (descriptor)\n",s->device.advertised_ms);
    else puts("Advertised interval: unknown");
    if (s->host_known) printf("Host parameter: %u (configured; application unverified)\n",s->host_raw);
    else puts("Host parameter: unknown");
    puts("Actual report rate: unmeasured");
    if (s->stored_known) printf("Stored interval code: 0x%02x (effect unverified)\n",s->stored_code);
    else puts("Stored interval code: unknown");
    if (s->battery_known) printf("Battery: %u%%, %s, state 0x%02x\n",s->battery,s->charging ? "charging" : "not charging",s->battery_state);
    else puts("Battery: unknown");
    puts("Slot  DPI X / Y      Raw bytes     State");
    for (unsigned i=0;i<CX_SLOTS;++i) {
        const CxSlot *p=&s->slots[i];
        printf(" %u %c  ",i,s->active_known && s->active_slot==i ? '*' : ' ');
        if (!p->known) { puts("unknown"); continue; }
        if (p->decoded) printf("%4u / %-4u ",p->dpi_x,p->dpi_y); else fputs("raw / unknown ",stdout);
        printf("%02x %02x %02x %02x  %s%s\n",p->raw[0],p->raw[1],p->raw[2],p->raw[3],
               p->empty ? "empty" : p->checksum_ok ? "checksum OK" : "BAD CHECKSUM",i>5 ? " / read-only" : "");
    }
    for (size_t i=0;i<s->warning_count;++i) printf("Warning: %s\n",s->warnings[i]);
}

void cx_print_mutation(const char *command,const CxMutation *m,const CxError *e,bool json) {
    if (json) {
        printf("{\"schema\":%d,\"command\":",CX_SCHEMA); cx_json_string(command);
        printf(",\"ok\":%s,\"changed\":%s,\"write_attempted\":%s,\"readback_verified\":%s,\"persistence_verified\":false,\"message\":",
               e->code==CX_OK ? "true" : "false",
               e->code!=CX_OK && m->write_attempted && !m->readback_verified ? "null" : m->changed ? "true" : "false",
               m->write_attempted ? "true" : "false",m->readback_verified ? "true" : "false");
        cx_json_string(e->code==CX_OK ? m->description : e->message);
        if (e->code!=CX_OK) { fputs(",\"error\":{\"message\":",stdout); cx_json_string(e->message); putchar('}'); }
        puts("}");
    } else if (e->code==CX_OK) puts(m->description);
    else {
        fprintf(stderr,"Error: %s\n",e->message);
        if (m->write_attempted) fputs("A write was attempted; inspect device state before repeating.\n",stderr);
    }
}

void cx_print_capabilities(bool json) {
    if (json) {
        printf("{\"schema\":%d,\"command\":\"capabilities\",\"supported\":[\"devices\",\"status\",\"battery\",\"dpi_read\",\"dpi_write_inactive_0_5_fa7b\",\"host_parameter\",\"experimental_interval_code\",\"probe\",\"reset\",\"tui\"],\"unimplemented_protocol\":[\"button_remapping\",\"macros\",\"rgb\",\"debounce\",\"lift_off_distance\"],\"live_packets\":\"hardware disabled: no demonstrated effect\"}\n",CX_SCHEMA);
    } else {
        puts("Read: USB discovery, battery/charging, DPI slots, active level, interval code, config dump.");
        puts("Write: inactive DPI slots 0..5 (fa7b only), experimental stored interval code.");
        puts("Host: global usbhid parameter (reconnect required); explicit whole-receiver USB reset.");
        puts("TUI: overview, DPI editor, rate settings, diagnostics, device picker, capabilities.");
        puts("Unknown protocol: button remapping, macros, RGB, debounce, lift-off distance.");
        puts("Live interval packets: hardware disabled; no demonstrated effect on verified unit.");
        puts("Sibling PIDs fa7c/fa03/fa93: read-only, individually unverified.");
    }
}
