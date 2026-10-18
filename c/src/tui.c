#include "compx.h"

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

typedef struct {
    CxOptions options;
    CxSnapshot snapshot;
    bool have_snapshot;
    CxDevice devices[CX_MAX_DEVICES]; size_t device_count;
    unsigned page, selected_slot, selected_device, dump_start;
    char dump[16][96]; unsigned dump_lines;
    char message[512];
} Screen;

static struct termios original;
static bool terminal_active;
static unsigned screen_columns=80;

static void restore(void) {
    if (!terminal_active) return;
    (void)tcsetattr(STDIN_FILENO,TCSANOW,&original);
    fputs("\033[0m\033[?25h\033[?1049l",stdout); fflush(stdout);
    terminal_active=false;
}

static int key(unsigned timeout) {
    struct pollfd p={.fd=STDIN_FILENO,.events=POLLIN};
    int rc=poll(&p,1,(int)timeout);
    if (rc<0) return errno==EINTR ? -1 : 'q';
    if (!rc) return -1;
    if (p.revents&(POLLHUP|POLLERR|POLLNVAL)) return 'q';
    unsigned char c;
    if (read(STDIN_FILENO,&c,1)!=1) return 'q';
    if (c!=27) return c;
    /* Bounded escape parsing; a standalone Escape cancels prompts. */
    struct pollfd more={.fd=STDIN_FILENO,.events=POLLIN};
    if (poll(&more,1,30)<=0 || read(STDIN_FILENO,&c,1)!=1 || c!='[') return 27;
    if (poll(&more,1,30)<=0 || read(STDIN_FILENO,&c,1)!=1) return 27;
    if (c=='A') return 1001;
    if (c=='B') return 1002;
    if (c=='C') return 1003;
    if (c=='D') return 1004;
    return 27;
}

static void at(unsigned row,unsigned col) { printf("\033[%u;%uH",row,col); }
static void line(unsigned row,const char *text) {
    at(row,4); fputs("\033[0m\033[2K",stdout);
    unsigned limit=screen_columns>5 ? screen_columns-5 : 0;
    for (unsigned i=0;text[i] && i<limit;++i) {
        unsigned char c=(unsigned char)text[i];
        putchar(c>=32 && c<127 ? c : '?');
    }
}

static void refresh_devices(Screen *s) {
    CxError e={0}; s->device_count=0;
    if (s->options.demo) { if (cx_choose(&s->options,&s->devices[0],&e)) s->device_count=1; }
    else if (!cx_discover(s->devices,CX_MAX_DEVICES,&s->device_count,&e)) snprintf(s->message,sizeof(s->message),"%s",e.message);
    if (s->selected_device>=s->device_count) s->selected_device=0;
}

static void refresh(Screen *s) {
    CxError e={0}; CxSnapshot fresh;
    s->have_snapshot=cx_snapshot(&s->options,&fresh,&e);
    if (s->have_snapshot) s->snapshot=fresh;
    else { memset(&s->snapshot,0,sizeof(s->snapshot)); snprintf(s->message,sizeof(s->message),"%s",e.message); }
}

static void draw(Screen *s) {
    struct winsize size={0}; (void)ioctl(STDOUT_FILENO,TIOCGWINSZ,&size);
    unsigned rows=size.ws_row ? size.ws_row : 24;
    unsigned cols=size.ws_col ? size.ws_col : 80;
    screen_columns=cols;
    fputs("\033[?25l\033[H\033[2J",stdout);
    if (rows<24 || cols<80) {
        line(2,"compxctl needs at least 80 columns x 24 rows. Resize, or press q."); fflush(stdout); return;
    }
    at(2,4); printf("\033[1;36mCOMPX\033[0m  /  Mouse control                         \033[33m%s\033[0m",s->options.demo ? "DEMO - NO HARDWARE" : "USB / LINUX");
    static const char *tabs[]={"Overview","DPI","Intervals","Diagnostics","Devices","Help"};
    at(4,4);
    for (unsigned i=0;i<6;++i) printf(i==s->page ? "\033[30;46m %u %s \033[0m " : "\033[90m %u %s \033[0m ",i+1,tabs[i]);
    char buf[512];
    if (s->page==4) {
        line(6,"Choose a receiver with Up/Down, then Enter. r rescans USB.");
        if (!s->device_count) line(8,"No supported receivers connected. Other pages remain available.");
        size_t first=s->selected_device>=10 ? s->selected_device-9 : 0;
        for (size_t i=first;i<s->device_count && i<first+10;++i) {
            snprintf(buf,sizeof(buf),"%c %-12s 25a7:%04x  %.80s",i==s->selected_device ? '>' : ' ',s->devices[i].id,s->devices[i].product,s->devices[i].name);
            line(8+(unsigned)(i-first),buf);
        }
    } else if (s->page==5) {
        line(6,"Controls");
        line(8,"1..6 / Tab: page   Up/Down: select   r: refresh   q: quit");
        line(10,"DPI: e edits the selected INACTIVE slot. Every write needs confirmation.");
        line(11,"Intervals: h sets the global host parameter; s stores a device code.");
        line(12,"Diagnostics: a chooses dump address; p reads 128 bytes; x resets USB.");
        line(14,"Read-only on sibling PIDs fa7c/fa03/fa93. Writes verified only on fa7b.");
        line(15,"RGB, button remapping, macros, debounce, lift-off: protocol unknown.");
        line(16,"Live interval packets: hardware disabled; no demonstrated effect.");
        line(18,"Readback verifies current bytes. Persistence needs a power-cycle test.");
        line(19,"No protocol sequence IDs: delayed matching replies remain ambiguous.");
    } else if (!s->have_snapshot) {
        line(7,"Device unavailable. Open 5 Devices, connect a receiver, then refresh.");
    } else {
        const CxSnapshot *p=&s->snapshot;
        snprintf(buf,sizeof(buf),"%.90s  /  USB %s",p->device.name,p->device.id); line(6,buf);
        if (s->page==0) {
            if (p->battery_known) snprintf(buf,sizeof(buf),"BATTERY    %u%%  %s",p->battery,p->charging ? "charging" : "not charging");
            else snprintf(buf,sizeof(buf),"BATTERY    unknown / sleeping or unavailable");
            line(8,buf);
            if (p->active_known && p->slots[p->active_slot].decoded) snprintf(buf,sizeof(buf),"DPI        slot %u  /  %u x %u",p->active_slot,p->slots[p->active_slot].dpi_x,p->slots[p->active_slot].dpi_y);
            else snprintf(buf,sizeof(buf),"DPI        unknown");
            line(10,buf);
            if (p->device.advertised_known) snprintf(buf,sizeof(buf),"USB        advertised interval %.3f ms",p->device.advertised_ms);
            else snprintf(buf,sizeof(buf),"USB        advertised interval unknown");
            line(12,buf);
            if (p->host_known) snprintf(buf,sizeof(buf),"HOST       configured raw parameter %u; application unverified",p->host_raw);
            else snprintf(buf,sizeof(buf),"HOST       parameter unavailable");
            line(14,buf);
            line(16,"Actual report frequency is UNMEASURED. No inferred 'effective Hz'.");
            line(18,"Read-only refresh every 3 seconds. No background writes.");
        } else if (s->page==1) {
            line(8,"    SLOT     X / Y DPI         BYTES            STATE");
            for (unsigned i=0;i<8;++i) {
                const CxSlot *slot=&p->slots[i]; char dpi[40];
                if (slot->decoded) snprintf(dpi,sizeof(dpi),"%u / %u",slot->dpi_x,slot->dpi_y); else strcpy(dpi,"unknown / raw");
                snprintf(buf,sizeof(buf),"%c   %u      %-17s %02x %02x %02x %02x   %s",i==s->selected_slot ? '>' : ' ',i,dpi,
                         slot->raw[0],slot->raw[1],slot->raw[2],slot->raw[3],
                         !slot->known ? "unavailable" : p->active_known && p->active_slot==i ? "ACTIVE / read-only" : i>5 ? "EXTENDED / read-only" : !p->active_known ? "active unknown / locked" : "editable");
                line(9+i,buf);
            }
            line(18,"Up/Down chooses slot. e edits. Active sensitivity changes by mouse button.");
        } else if (s->page==2) {
            line(8,"HOST PARAMETER       h: configure 0 / 125 / 500 / 1000");
            line(9,"Global setting; requires root. Reconnect affected mice to apply.");
            line(10,"HZ mapping is for full-speed endpoints; other USB mice may differ.");
            if (p->host_known) snprintf(buf,sizeof(buf),"Current raw host parameter: %u (0 = descriptor default)",p->host_raw);
            else strcpy(buf,"Current raw host parameter: unknown");
            line(12,buf);
            line(14,"DEVICE CODE          s: store experimental interval code");
            if (p->stored_known) snprintf(buf,sizeof(buf),"Current stored code: 0x%02x",p->stored_code);
            else strcpy(buf,"Current stored code: unknown");
            line(15,buf);
            line(17,"Stored code readback is verified; rate effect is NOT demonstrated.");
            line(18,"Power-off persistence is unverified. No automatic USB reset.");
        } else if (s->page==3) {
            snprintf(buf,sizeof(buf),"Read-only config memory  /  start 0x%04x   a: address   p: read",s->dump_start); line(8,buf);
            if (!s->dump_lines) line(10,"Press p to read 128 bytes. No arbitrary memory writes are exposed.");
            for (unsigned i=0;i<s->dump_lines && i<8;++i) line(10+i,s->dump[i]);
            line(19,"x: reset WHOLE receiver (keyboard too). Optional USB permission required.");
        }
        if (p->warning_count) {
            snprintf(buf,sizeof(buf),"Read warning: %.150s",p->warnings[0]); line(20,buf);
        }
    }
    /* Bound status text to terminal width; user/OS error strings cannot wrap
     * into the command hint or escape the UI using control characters. */
    at(rows-2,4); fputs("\033[33m",stdout);
    size_t limit=cols>8 ? cols-8 : 0;
    for (size_t i=0;s->message[i] && i<limit;++i) {
        unsigned char c=(unsigned char)s->message[i]; putchar(c>=32 && c<127 ? c : '?');
    }
    at(rows,4); fputs("\033[90mTab / 1..6  navigate     r refresh     q quit\033[0m",stdout); fflush(stdout);
}

static bool prompt(Screen *s,const char *label,char *out,size_t capacity) {
    size_t n=0; out[0]=0;
    for (;;) {
        if (cx_signal) return false;
        snprintf(s->message,sizeof(s->message),"%s: %s  [Enter accepts, Esc cancels]",label,out);
        draw(s);
        int c=key(200);
        if (c=='\r' || c=='\n') return n>0;
        if (c==27 || c==3 || c=='q') { strcpy(s->message,"Cancelled; no changes sent."); return false; }
        if ((c==127 || c==8) && n) out[--n]=0;
        else if (((c>='0' && c<='9') || c=='x' || c=='X' || (c>='a' && c<='f') || (c>='A' && c<='F')) && n+1<capacity) {
            out[n++]=(char)c; out[n]=0;
        }
    }
}

static bool confirm(Screen *s,const char *description) {
    snprintf(s->message,sizeof(s->message),"%s  [y confirms; other key cancels]",description);
    draw(s);
    /* Discard queued keystrokes: a held key must not approve a new operation. */
    (void)tcflush(STDIN_FILENO,TCIFLUSH);
    while (!cx_signal) {
        int c=key(200);
        if (c<0) continue;
        if (c=='y' || c=='Y') return true;
        strcpy(s->message,"Cancelled; no changes sent."); return false;
    }
    return false;
}

static void mutation(Screen *s,char action) {
    CxError e={0}; CxMutation result={0}; char input[32]; unsigned value=0;
    if (action=='e' && s->selected_slot>5) { strcpy(s->message,"Extended slots are read-only."); return; }
    if (action=='e' || action=='h' || action=='s') {
        const char *label=action=='e' ? "DPI (50..6400, step 50)" : "Rate (125/500/1000; host also accepts 0)";
        if (!prompt(s,label,input,sizeof(input))) return;
        if (!cx_number(input,0,6400,&value) || (action=='e' ? value<50 || value%50 : value!=125 && value!=500 && value!=1000 && !(action=='h' && value==0))) {
            strcpy(s->message,"Invalid value; nothing written."); return;
        }
    }
    char review[256];
    if (action=='e') snprintf(review,sizeof(review),"Write slot %u = %u DPI? Do not press the mouse DPI button during write.",s->selected_slot,value);
    else if (action=='h') snprintf(review,sizeof(review),"GLOBAL host parameter for %u Hz? Root needed; reconnect mice; actual rate unmeasured.",value);
    else if (action=='s') snprintf(review,sizeof(review),"EXPERIMENTAL: store interval code for %u? Rate effect/persistence unverified.",value);
    else strcpy(review,"Reset WHOLE USB receiver? Mouse and any paired keyboard may disconnect.");
    if (!confirm(s,review)) return;
    if (action=='h') (void)cx_host_rate(value,s->options.demo,&result,&e);
    else {
        CxDevice device;
        if (!cx_choose(&s->options,&device,&e)) goto finished;
        if (action=='x') (void)cx_reset(&device,s->options.demo,&result,&e);
        else {
            CxSession session;
            if (!cx_open(&session,&device,&s->options,&e)) goto finished;
            if (action=='e') (void)cx_set_dpi(&session,s->selected_slot,value,&result,&e);
            else (void)cx_set_interval(&session,value,&result,&e);
            cx_close(&session);
        }
    }
finished:;
    char message[512];
    snprintf(message,sizeof(message),"%s%s",e.code==CX_OK ? result.description : e.message,
             e.code!=CX_OK && result.write_attempted ? " / WRITE ATTEMPTED: state uncertain" : "");
    refresh(s);
    snprintf(s->message,sizeof(s->message),"%s",message);
}

static void dump(Screen *s) {
    CxDevice device; CxSession session; CxError e={0};
    s->dump_lines=0;
    if (!cx_choose(&s->options,&device,&e) || !cx_open(&session,&device,&s->options,&e)) goto error;
    /* Eight rows of sixteen bytes, each transaction respects the 10-byte limit. */
    for (unsigned i=0;i<8 && s->dump_start+16*i<65536;++i) {
        unsigned address=s->dump_start+16*i;
        unsigned length=65536-address<16 ? 65536-address : 16;
        uint8_t bytes[16];
        unsigned offset=0;
        while (offset<length) {
            unsigned chunk=length-offset<8 ? length-offset : 8;
            if (!cx_read(&session,address+offset,chunk,bytes+offset,&e)) { cx_close(&session); goto error; }
            offset+=chunk;
        }
        int n=snprintf(s->dump[i],sizeof(s->dump[i]),"%04x  ",address);
        for (unsigned j=0;j<length;++j) n+=snprintf(s->dump[i]+n,sizeof(s->dump[i])-(size_t)n,"%02x ",bytes[j]);
        ++s->dump_lines;
    }
    cx_close(&session); strcpy(s->message,"Read-only dump complete."); return;
error:
    snprintf(s->message,sizeof(s->message),"Dump incomplete: %s",e.message);
}

int cx_tui(const CxOptions *options) {
    if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO)) {
        fputs("tui requires an interactive terminal; use status --json or tui --demo in a terminal.\n",stderr); return 2;
    }
    if (tcgetattr(STDIN_FILENO,&original)!=0) { perror("tcgetattr"); return 1; }
    struct termios raw=original;
    raw.c_lflag &= (tcflag_t)~(ICANON|ECHO); raw.c_iflag &= (tcflag_t)~(IXON|ICRNL);
    raw.c_cc[VMIN]=1; raw.c_cc[VTIME]=0;
    if (tcsetattr(STDIN_FILENO,TCSANOW,&raw)!=0) { perror("tcsetattr"); return 1; }
    terminal_active=true; (void)atexit(restore);
    fputs("\033[?1049h",stdout);
    Screen *s=calloc(1,sizeof(*s));
    if (!s) { restore(); fputs("Out of memory\n",stderr); return 1; }
    s->options=*options;
    strcpy(s->message,options->demo ? "SIMULATED data. Changes live in memory only." : "Read-only until an action is explicitly confirmed.");
    refresh_devices(s);
    if (!options->device_id[0] && s->device_count>1) s->page=4;
    refresh(s);
    int64_t next=cx_now_ms()+3000;
    while (!cx_signal) {
        draw(s);
        int c=key(200);
        if (c=='q') break;
        if (c>='1' && c<='6') s->page=(unsigned)(c-'1');
        else if (c=='\t' || c==1003) s->page=(s->page+1)%6;
        else if (c==1004) s->page=(s->page+5)%6;
        else if (c=='r') { refresh_devices(s); refresh(s); next=cx_now_ms()+3000; }
        else if (c==1001 || c=='k') {
            if (s->page==1) s->selected_slot=(s->selected_slot+7)%8;
            else if (s->page==4 && s->device_count) s->selected_device=(s->selected_device+(unsigned)s->device_count-1)%(unsigned)s->device_count;
        } else if (c==1002 || c=='j') {
            if (s->page==1) s->selected_slot=(s->selected_slot+1)%8;
            else if (s->page==4 && s->device_count) s->selected_device=(s->selected_device+1)%(unsigned)s->device_count;
        } else if ((c=='\r' || c=='\n') && s->page==4 && s->device_count) {
            snprintf(s->options.device_id,sizeof(s->options.device_id),"%s",s->devices[s->selected_device].id);
            s->page=0; refresh(s); s->dump_lines=0;
        } else if (s->page==1 && c=='e') mutation(s,'e');
        else if (s->page==2 && (c=='h' || c=='s')) mutation(s,(char)c);
        else if (s->page==3 && c=='x') mutation(s,'x');
        else if (s->page==3 && c=='p') dump(s);
        else if (s->page==3 && c=='a') {
            char input[32]; unsigned value;
            if (prompt(s,"Start address (0..0xffff)",input,sizeof(input)) && cx_number(input,0,65535,&value)) {
                s->dump_start=value; s->dump_lines=0;
            }
        }
        if (cx_now_ms()>=next && s->page<3) { refresh(s); next=cx_now_ms()+3000; }
    }
    free(s); restore(); return cx_signal ? 128+cx_signal : 0;
}
