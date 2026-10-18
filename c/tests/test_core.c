#include "compx.h"

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static unsigned checks;
#define CHECK(expr) do { ++checks; if (!(expr)) { fprintf(stderr,"FAIL %s:%d: %s\n",__FILE__,__LINE__,#expr); abort(); } } while (0)

static void reply(uint8_t out[CX_FRAME],unsigned addr,const uint8_t *data,unsigned n) {
    CxError e={0}; CHECK(cx_read_frame(addr,n,out,&e)); out[0]=9;
    memcpy(out+6,data,n); out[16]=cx_checksum(out,16);
}

static void protocol(void) {
    CxError e={0}; uint8_t out[17];
    CHECK(cx_read_frame(0xffff,1,out,&e));
    CHECK(!cx_read_frame(0xffff,2,out,&e));
    CHECK(!cx_read_frame(65536,1,out,&e));
    CHECK(!cx_read_frame(0,0,out,&e));
    CHECK(!cx_read_frame(0,11,out,&e));
    uint8_t data[]={1,0x54};
    const uint8_t known[]={8,7,0,0,0,2,1,0x54,0,0,0,0,0,0,0,0,0xef};
    CHECK(cx_write_frame(0,data,2,out,&e)); CHECK(!memcmp(out,known,17));
    CHECK(!cx_write_frame(65535,data,2,out,&e));
    reply(out,0,data,2); CHECK(cx_valid_frame(out,17));
    CHECK(!cx_valid_frame(out,16)); out[10]^=1; CHECK(!cx_valid_frame(out,17));
    for (unsigned dpi=50;dpi<=6400;dpi+=50) {
        uint8_t raw[4]; CHECK(cx_dpi_payload(dpi,raw,&e));
        CxSlot s=cx_slot(raw); CHECK(s.decoded && s.dpi_x==dpi && s.dpi_y==dpi && s.checksum_ok);
    }
    CHECK(!cx_dpi_payload(0,out,&e)); CHECK(!cx_dpi_payload(51,out,&e));
    uint8_t corrupt[4]={7,7,0,0}; CHECK(!cx_slot(corrupt).decoded);
    uint8_t asymmetric[4]={7,15,0,0}; asymmetric[3]=cx_checksum(asymmetric,3);
    CHECK(cx_slot(asymmetric).dpi_x==400 && cx_slot(asymmetric).dpi_y==800);
}

static void descriptors(void) {
    uint8_t valid[]={0x75,8,0x95,16,0x85,8,0xb1,2,0x85,9,0x81,2};
    CxDescriptor d; CxError e={0};
    /* Config-interface descriptor captured by the original project on fa7b. */
    static const char capture[]=
        "05010906a1018501050719e029e715002501750195088102050719002aff0095"
        "067508150026ff008100c00603ff0900a10185020900150026ff007508950781"
        "02c0050c0901a10185051500263c0219002a3c02751095018100c005010980a1"
        "0185031981298315002501950375018102950175058101c00601ff0900a10185"
        "09090095107508150026ff008102c00604ff0902a10185060902150026ff0075"
        "089507b102c00602ff0902a10185080902150026ff0075089510b102c0";
    uint8_t captured[(sizeof(capture)-1)/2];
    for (size_t i=0;i<sizeof(captured);++i) {
        char pair[3]={capture[2*i],capture[2*i+1],0};
        captured[i]=(uint8_t)strtoul(pair,NULL,16);
    }
    CHECK(cx_descriptor(captured,sizeof(captured),&d,&e));
    CHECK(d.feature8 && d.input9 && d.feature6 && d.feature8_bits==128 && d.feature6_bits==56);
    CHECK(cx_descriptor(valid,sizeof(valid),&d,&e) && d.feature8 && d.input9);
    uint8_t pushed[]={0x75,8,0x95,16,0x85,8,0xa4,0x85,1,0x81,0,0xb4,0xb1,0,0x85,9,0x81,0};
    CHECK(cx_descriptor(pushed,sizeof(pushed),&d,&e) && d.feature8 && d.input9);
    uint8_t underflow[]={0xb4}; CHECK(!cx_descriptor(underflow,sizeof(underflow),&d,&e));
    uint8_t truncated[]={0x85}; CHECK(!cx_descriptor(truncated,sizeof(truncated),&d,&e));
    uint8_t overflow[]={0x75,32,0x97,255,255,255,255,0x81,0};
    CHECK(!cx_descriptor(overflow,sizeof(overflow),&d,&e));
    valid[3]=15; CHECK(cx_descriptor(valid,sizeof(valid),&d,&e) && !d.feature8 && !d.input9);
    uint32_t random=12345;
    for (unsigned i=0;i<20000;++i) {
        uint8_t bytes[128];
        for (size_t j=0;j<sizeof(bytes);++j) { random=random*1664525u+1013904223u; bytes[j]=(uint8_t)(random>>24); }
        (void)cx_descriptor(bytes,i%sizeof(bytes),&d,&e);
    }
}

typedef struct { int reader,writer; unsigned sent; bool wrong,ack,continuous; uint8_t value[2]; } Wire;
static bool wire_send(void *ctx,const uint8_t *p,size_t n,CxError *e) {
    (void)p; (void)e; Wire *w=ctx; ++w->sent; CHECK(n==17);
    uint8_t answer[17];
    if (w->ack) { reply(answer,0,w->value,2); answer[1]=7; answer[16]=cx_checksum(answer,16); CHECK(send(w->writer,answer,17,0)==17); }
    reply(answer,w->wrong ? 4 : 0,w->value,2); CHECK(send(w->writer,answer,17,0)==17); return true;
}
static int wire_receive(void *ctx,uint8_t *buf,size_t size,unsigned timeout,CxError *e) {
    Wire *w=ctx;
    if (w->continuous) { memset(buf,0,size); return 1; }
    return cx_receive_fd(&w->reader,buf,size,timeout,e);
}
static void transport(void) {
    int sockets[2]; CHECK(socketpair(AF_UNIX,SOCK_DGRAM,0,sockets)==0);
    CHECK(fcntl(sockets[0],F_SETFL,O_NONBLOCK)==0);
    Wire w={.reader=sockets[0],.writer=sockets[1],.ack=true,.value={2,0x53}};
    CxSession s={.transport={&w,wire_receive,wire_send},.timeout_ms=20,.tries=2};
    CxError e={0}; uint8_t stale[17],old[]={1,0x54},out[2],buffer[128];
    CHECK(send(sockets[1],"test",4,0)==4);
    CHECK(cx_receive_fd(&w.reader,buffer,sizeof(buffer),0,&e)==4); /* real zero-time read */
    CHECK(cx_receive_fd(&w.reader,buffer,sizeof(buffer),0,&e)==0);
    reply(stale,0,old,2); CHECK(send(sockets[1],stale,17,0)==17);
    CHECK(cx_read(&s,0,2,out,&e)); CHECK(out[0]==2 && out[1]==0x53 && w.sent==1);
    w.wrong=true;
    int64_t before=cx_now_ms(); CHECK(!cx_read(&s,0,2,out,&e));
    CHECK(cx_now_ms()-before<500 && e.code==CX_PROTOCOL && !s.silent);
    w.continuous=true; CHECK(!cx_drain(&s,&e) && e.code==CX_PROTOCOL);
    w.continuous=false;
    cx_signal=15; CHECK(cx_receive_fd(&w.reader,buffer,sizeof(buffer),0,&e)<0 && e.code==CX_INTERRUPTED); cx_signal=0;
    close(sockets[0]); close(sockets[1]);
}

typedef struct {
    uint8_t memory[64],queued[17]; bool ready,unknown_active,activate_on_recheck,corrupt_back,fail_readback;
    unsigned writes,active_reads;
} Memory;
static bool memory_send(void *ctx,const uint8_t *p,size_t n,CxError *e) {
    Memory *m=ctx; CHECK(n==17);
    unsigned address=((unsigned)p[3]<<8)|p[4],length=p[5]; CHECK(address+length<=64);
    if (p[1]==7) { ++m->writes; memcpy(m->memory+address,p+6,length); return true; }
    if (m->fail_readback && m->writes) return true;
    uint8_t data[10]; memcpy(data,m->memory+address,length);
    if (address==4) {
        ++m->active_reads;
        if (m->unknown_active) { data[0]=255;data[1]=255; }
        if (m->activate_on_recheck && m->active_reads>1) { data[0]=2;data[1]=0x53; }
    }
    if (m->corrupt_back && m->writes) data[0]^=1;
    reply(m->queued,address,data,length); m->ready=true; (void)e; return true;
}
static int memory_receive(void *ctx,uint8_t *buf,size_t size,unsigned timeout,CxError *e) {
    Memory *m=ctx; (void)size; (void)timeout; (void)e;
    if (!m->ready) return 0;
    memcpy(buf,m->queued,17); m->ready=false; return 17;
}
static CxSession memory_session(Memory *m) {
    memset(m,0,sizeof(*m)); m->memory[4]=1;m->memory[5]=0x54;
    CxError e={0}; for (unsigned i=0;i<6;++i) CHECK(cx_dpi_payload(400,m->memory+12+4*i,&e));
    CxSession s={.transport={m,memory_receive,memory_send},.timeout_ms=10,.tries=1};
    s.device.product=0xfa7b; return s;
}
static void operations(void) {
    Memory m; CxSession s=memory_session(&m); CxError e={0}; CxMutation change;
    CHECK(!cx_set_dpi(&s,0,800,&change,&e) && m.writes==0);
    CHECK(cx_set_dpi(&s,1,800,&change,&e) && m.writes==1 && change.readback_verified);
    CHECK(cx_set_dpi(&s,1,800,&change,&e) && !change.changed && !change.write_attempted && m.writes==1);
    CHECK(!cx_set_dpi(&s,6,800,&change,&e) && m.writes==1);
    s=memory_session(&m);m.unknown_active=true;
    CHECK(!cx_set_dpi(&s,1,800,&change,&e) && m.writes==0);
    s=memory_session(&m);m.activate_on_recheck=true;
    CHECK(!cx_set_dpi(&s,1,800,&change,&e) && m.writes==0);
    s=memory_session(&m);m.corrupt_back=true;
    CHECK(!cx_set_dpi(&s,1,800,&change,&e) && change.write_attempted && !change.readback_verified && m.writes==1);
    s=memory_session(&m);m.fail_readback=true;
    CHECK(!cx_set_dpi(&s,1,800,&change,&e) && change.write_attempted && m.writes==1);
    s=memory_session(&m);s.device.product=0xfa7c;
    CHECK(!cx_set_dpi(&s,1,800,&change,&e) && m.writes==0);
    s=memory_session(&m);m.memory[0]=1;m.memory[1]=0x54;
    CHECK(cx_set_interval(&s,1000,&change,&e) && m.writes==0);
    CHECK(cx_set_interval(&s,500,&change,&e) && m.writes==1 && m.memory[0]==2 && m.memory[1]==0x53);
    CHECK(m.memory[4]==1 && m.memory[5]==0x54);
}

int main(void) {
    protocol(); descriptors(); transport(); operations();
    printf("Core: %u assertions, 20000 malformed-descriptor samples, real socket transport OK\n",checks);
    return 0;
}
