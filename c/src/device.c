#include "compx.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

bool cx_read(CxSession *s,unsigned addr,unsigned length,uint8_t *out,CxError *e) {
    uint8_t command[CX_FRAME],reply[CX_FRAME];
    if (!cx_read_frame(addr,length,command,e) || !cx_request(s,command,reply,e)) return false;
    memcpy(out,reply+6,length); return true;
}

bool cx_battery(CxSession *s,unsigned *percent,bool *charging,unsigned *state,CxError *e) {
    uint8_t command[CX_FRAME]={8,4},reply[CX_FRAME];
    command[16]=cx_checksum(command,16);
    if (!cx_request(s,command,reply,e)) return false;
    if (reply[6]>100 || reply[7]>1) {
        cx_error(e,CX_PROTOCOL,"Invalid battery percentage or charging flag"); return false;
    }
    *percent=reply[6]; *charging=reply[7]!=0; *state=reply[5]; return true;
}

static bool active(CxSession *s,unsigned *slot,CxError *e) {
    uint8_t raw[2];
    if (!cx_read(s,4,2,raw,e)) return false;
    if (raw[0]<1 || raw[0]>8 || cx_checksum(raw,2)!=0) {
        cx_error(e,CX_PROTOCOL,"Active DPI level unknown or checksum invalid; writes refused"); return false;
    }
    *slot=(unsigned)raw[0]-1; return true;
}

static void warning(CxSnapshot *out,const CxError *e) {
    if (out->warning_count<8) {
        snprintf(out->warnings[out->warning_count],sizeof(out->warnings[0]),"%s",e->message);
        ++out->warning_count;
    }
}

bool cx_snapshot(const CxOptions *o,CxSnapshot *out,CxError *e) {
    memset(out,0,sizeof(*out));
    if (!cx_choose(o,&out->device,e)) return false;
    if (o->demo) { out->host_known=true; out->host_raw=0; }
    else {
        FILE *f=fopen("/sys/module/usbhid/parameters/mousepoll","re");
        if (f) { out->host_known=fscanf(f,"%u",&out->host_raw)==1; fclose(f); }
    }
    CxSession s;
    CxError read_error={0};
    if (!cx_open(&s,&out->device,o,&read_error)) {
        if (read_error.code==CX_INTERRUPTED) { *e=read_error; return false; }
        warning(out,&read_error); return true;
    }
    uint8_t raw[8];
    if (cx_read(&s,0,2,raw,&read_error)) {
        if (cx_checksum(raw,2)==0) { out->stored_known=true; out->stored_code=raw[0]; }
        else { cx_error(&read_error,CX_PROTOCOL,"Stored interval checksum invalid"); warning(out,&read_error); }
    } else warning(out,&read_error);
    if (active(&s,&out->active_slot,&read_error)) out->active_known=true;
    else warning(out,&read_error);
    for (unsigned i=0;i<CX_SLOTS;i+=2) {
        if (cx_read(&s,12+4*i,8,raw,&read_error)) {
            out->slots[i]=cx_slot(raw); out->slots[i+1]=cx_slot(raw+4);
        } else { warning(out,&read_error); if (s.silent || cx_signal) break; }
    }
    out->battery_known=cx_battery(&s,&out->battery,&out->charging,&out->battery_state,&read_error);
    if (!out->battery_known) warning(out,&read_error);
    cx_close(&s);
    if (cx_signal) { cx_error(e,CX_INTERRUPTED,"Interrupted while reading device"); return false; }
    return true;
}

static bool writable(CxSession *s,CxError *e) {
    if (s->device.product!=0xfa7b) {
        cx_error(e,CX_REFUSED,"Writes restricted to verified PID fa7b; sibling firmware is read-only"); return false;
    }
    return true;
}

static bool write_verified(CxSession *s,unsigned addr,const uint8_t *data,unsigned n,
                           CxMutation *m,CxError *e) {
    uint8_t command[CX_FRAME],back[10];
    if (!cx_write_frame(addr,data,n,command,e) || !cx_drain(s,e)) return false;
    if (cx_signal) { cx_error(e,CX_INTERRUPTED,"Interrupted before write"); return false; }
    m->write_attempted=true;
    /* Never retry a write automatically: failure can mean unknown delivery. */
    if (!s->transport.send(s->transport.context,command,CX_FRAME,e)) return false;
    m->changed=true;
    if (!cx_read(s,addr,n,back,e)) return false;
    if (memcmp(back,data,n)) {
        cx_error(e,CX_PROTOCOL,"Write sent but readback differs; actual device state uncertain"); return false;
    }
    m->readback_verified=true; return true;
}

bool cx_set_dpi(CxSession *s,unsigned slot,unsigned dpi,CxMutation *m,CxError *e) {
    memset(m,0,sizeof(*m));
    uint8_t payload[4],before[4]; unsigned current;
    if (!writable(s,e) || !cx_dpi_payload(dpi,payload,e)) return false;
    if (slot>5) { cx_error(e,CX_REFUSED,"Only DPI slots 0..5 are writable; extended slots 6..7 are read-only"); return false; }
    if (!active(s,&current,e)) return false;
    if (slot==current) {
        cx_error(e,CX_REFUSED,"Slot %u is active; change level with the mouse DPI button before writing",slot); return false;
    }
    if (!cx_read(s,12+4*slot,4,before,e)) return false;
    if (!memcmp(payload,before,4)) {
        m->readback_verified=true;
        snprintf(m->description,sizeof(m->description),"Slot %u already contains %u DPI; nothing written",slot,dpi); return true;
    }
    /* Recheck immediately before mutation; a physical button cannot be locked by
     * flock, so an unavoidable residual race is documented, not hidden. */
    if (!active(s,&current,e)) return false;
    if (slot==current) { cx_error(e,CX_REFUSED,"Selected slot became active; write cancelled"); return false; }
    if (!write_verified(s,12+4*slot,payload,4,m,e)) return false;
    snprintf(m->description,sizeof(m->description),
             "Slot %u: %u DPI, bytes read back. Power-cycle persistence unverified; current sensitivity unchanged.",slot,dpi);
    return true;
}

static unsigned interval_code(unsigned rate) {
    switch (rate) { case 125:return 8; case 500:return 2; case 1000:return 1; default:return 0; }
}

bool cx_set_interval(CxSession *s,unsigned rate,CxMutation *m,CxError *e) {
    memset(m,0,sizeof(*m));
    if (!writable(s,e)) return false;
    unsigned code=interval_code(rate);
    if (!code) { cx_error(e,CX_REFUSED,"Interval code supports only 125, 500, 1000"); return false; }
    uint8_t payload[2]={(uint8_t)code,(uint8_t)(0x55-code)},before[2];
    if (!cx_read(s,0,2,before,e)) return false;
    if (!memcmp(payload,before,2)) {
        m->readback_verified=true;
        strcpy(m->description,"Stored interval code already matches; nothing written. Actual effect unverified."); return true;
    }
    if (!write_verified(s,0,payload,2,m,e)) return false;
    strcpy(m->description,"Interval code written and read back. Effect on report rate and persistence after power-off unverified.");
    return true;
}

bool cx_live_interval(CxSession *s,unsigned rate,CxMutation *m,CxError *e) {
    memset(m,0,sizeof(*m));
    if (!writable(s,e)) return false;
    unsigned code=interval_code(rate);
    if (!code) { cx_error(e,CX_REFUSED,"Live interval supports only 125, 500, 1000"); return false; }
    /* Verify report 6 separately on the actual opened interface. */
    if (!s->demo) {
        cx_error(e,CX_REFUSED,"Live packets have no demonstrated effect; use stored interval or investigate captures instead");
        return false;
    }
    uint8_t first[8]={6,0x11,0,0,0,0,0,0},second[17]={8,0x11,0,0,0,6};
    first[3]=(uint8_t)(rate==1000 ? 2 : rate==500 ? 1 : 0); second[6]=(uint8_t)code;
    m->write_attempted=true;
    if (!s->transport.send(s->transport.context,first,8,e) ||
        !s->transport.send(s->transport.context,second,17,e)) return false;
    m->changed=true;
    strcpy(m->description,"SIMULATED live packets. Hardware execution disabled: no demonstrated effect."); return true;
}
