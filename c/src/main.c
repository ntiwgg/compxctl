#include "compx.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void caught(int signo) { cx_signal=signo; }

static void usage(void) {
    puts("compxctl " CX_VERSION " - Linux CompX mouse control\n"
         "\nCommands:\n"
         "  tui                         interactive terminal (default on a TTY)\n"
         "  devices                     list connected supported receivers\n"
         "  status                      read snapshot; partial errors become warnings\n"
         "  battery                     read battery and charging state\n"
         "  dpi [list]                  list all DPI slots\n"
         "  dpi set SLOT DPI --confirm  write inactive slot 0..5, steps of 50\n"
         "  rate                        show advertised/configured values (not measured)\n"
         "  rate host HZ --confirm      global parameter; 0/125/500/1000; reconnect required\n"
         "  rate device HZ --persist --confirm  experimental stored interval code\n"
         "  probe [--start ADDR] [--length BYTES]  read-only config dump\n"
         "  reset --confirm             reset entire receiver, including any keyboard\n"
         "  capabilities                supported and unknown firmware functions\n"
         "\nOptions (before or after command):\n"
         "  --device USB-ID  --json  --timeout-ms 1..5000  --tries 1..8\n"
         "  --demo          simulated device; no hardware or host writes\n"
         "  --version       version\n"
         "\nHost HZ mapping is for full-speed USB. Parameter is global and encoded\n"
         "differently for high-speed endpoints; it is not a measured frequency.\n"
         "Unknown device functions (RGB/buttons/macros) are not guessed.");
}

static int fail(const char *command,CxError *e,const CxOptions *o,int code) {
    CxMutation m={0}; cx_print_mutation(command,&m,e,o->json);
    return cx_signal ? 128+cx_signal : code;
}

int main(int argc,char **argv) {
    struct sigaction sa={0}; sa.sa_handler=caught; sigemptyset(&sa.sa_mask);
    (void)sigaction(SIGINT,&sa,NULL); (void)sigaction(SIGTERM,&sa,NULL);
    CxOptions o={.timeout_ms=250,.tries=2};
    /* Select error rendering before parsing: an early invalid option must still
     * produce JSON when --json appears later in argv. */
    for (int i=1;i<argc;++i) if (!strcmp(argv[i],"--json")) o.json=true;
    bool confirm=false,persist=false; unsigned start=0,length=64;
    const char *args[5]={0}; size_t n=0;
    CxError e={0};
    for (int i=1;i<argc;++i) {
        const char *a=argv[i];
        if (!strcmp(a,"--json")) o.json=true;
        else if (!strcmp(a,"--demo")) o.demo=true;
        else if (!strcmp(a,"--confirm")) confirm=true;
        else if (!strcmp(a,"--persist")) persist=true;
        else if (!strcmp(a,"--help") || !strcmp(a,"-h")) { usage(); return 0; }
        else if (!strcmp(a,"--version")) { puts("compxctl " CX_VERSION); return 0; }
        else if (!strcmp(a,"--device") || !strcmp(a,"--timeout-ms") || !strcmp(a,"--tries") ||
                 !strcmp(a,"--start") || !strcmp(a,"--length")) {
            if (++i==argc || !strncmp(argv[i],"--",2)) { cx_error(&e,CX_REFUSED,"Option %s needs a value",a); goto argument_error; }
            if (!strcmp(a,"--device")) {
                if (!argv[i][0] || strlen(argv[i])>=sizeof(o.device_id)) { cx_error(&e,CX_REFUSED,"Invalid device ID"); goto argument_error; }
                strcpy(o.device_id,argv[i]);
            } else {
                unsigned value,maximum=!strcmp(a,"--tries") ? 8 : !strcmp(a,"--timeout-ms") ? 5000 : 65536;
                unsigned minimum=!strcmp(a,"--start") ? 0 : 1;
                if (!cx_number(argv[i],minimum,maximum,&value)) { cx_error(&e,CX_REFUSED,"Invalid value for %s",a); goto argument_error; }
                if (!strcmp(a,"--tries")) o.tries=value;
                else if (!strcmp(a,"--timeout-ms")) o.timeout_ms=value;
                else if (!strcmp(a,"--start")) start=value;
                else length=value;
            }
        } else if (a[0]=='-' || n==5) { cx_error(&e,CX_REFUSED,"Unknown option or too many arguments: %s",a); goto argument_error; }
        else args[n++]=a;
    }
    const char *cmd=n ? args[0] : isatty(STDIN_FILENO) && isatty(STDOUT_FILENO) ? "tui" : "status";
    if (!strcmp(cmd,"tui")) {
        if (n>1 || o.json) { cx_error(&e,CX_REFUSED,"tui does not accept positional arguments or --json"); return fail(cmd,&e,&o,2); }
        return cx_tui(&o);
    }
    if (!strcmp(cmd,"capabilities")) {
        if (n>1) goto invalid_command;
        cx_print_capabilities(o.json); return 0;
    }
    if (!strcmp(cmd,"devices")) {
        if (n>1) goto invalid_command;
        CxDevice devices[CX_MAX_DEVICES]; size_t count;
        if (o.demo) { if (!cx_choose(&o,&devices[0],&e)) return fail(cmd,&e,&o,1); count=1; }
        else if (!cx_discover(devices,CX_MAX_DEVICES,&count,&e)) return fail(cmd,&e,&o,1);
        if (o.json) printf("{\"schema\":%d,\"command\":\"devices\",\"devices\":[",CX_SCHEMA);
        for (size_t i=0;i<count;++i) {
            if (o.json) {
                if (i) putchar(',');
                fputs("{\"id\":",stdout); cx_json_string(devices[i].id);
                fputs(",\"name\":",stdout); cx_json_string(devices[i].name);
                printf(",\"product\":\"%04x\",\"node\":",devices[i].product); cx_json_string(devices[i].node); putchar('}');
            } else printf("%-12s 25a7:%04x  %s  %s\n",devices[i].id,devices[i].product,devices[i].name,
                          devices[i].node[0] ? devices[i].node : "config interface unavailable");
        }
        if (o.json) puts("]}"); else if (!count) puts("No supported USB receivers found.");
        return 0;
    }
    if ((!strcmp(cmd,"status") || !strcmp(cmd,"rate")) && n<=1) {
        CxSnapshot snapshot;
        if (!cx_snapshot(&o,&snapshot,&e)) return fail(cmd,&e,&o,1);
        cx_print_snapshot(&snapshot,o.json); return 0;
    }
    if (!strcmp(cmd,"dpi") && (n<=1 || (n==2 && !strcmp(args[1],"list")))) {
        CxSnapshot snapshot;
        if (!cx_snapshot(&o,&snapshot,&e)) return fail(cmd,&e,&o,1);
        cx_print_snapshot(&snapshot,o.json); return 0;
    }
    if (!strcmp(cmd,"rate") && n==3 && !strcmp(args[1],"host")) {
        unsigned rate;
        if (!cx_number(args[2],0,1000,&rate)) goto invalid_command;
        if (!confirm) { cx_error(&e,CX_REFUSED,"Global host change requires --confirm; affects other mice, reconnect required"); return fail(cmd,&e,&o,1); }
        CxMutation m={0}; (void)cx_host_rate(rate,o.demo,&m,&e);
        cx_print_mutation(cmd,&m,&e,o.json); return cx_signal ? 128+cx_signal : e.code!=CX_OK;
    }
    bool set_dpi=!strcmp(cmd,"dpi") && n==4 && !strcmp(args[1],"set");
    bool set_rate=!strcmp(cmd,"rate") && n==3 && !strcmp(args[1],"device");
    bool live=!strcmp(cmd,"experimental") && n==3 && !strcmp(args[1],"live");
    bool reset=!strcmp(cmd,"reset") && n==1;
    bool probe=!strcmp(cmd,"probe") && n==1;
    bool battery=!strcmp(cmd,"battery") && n==1;
    if (!set_dpi && !set_rate && !live && !reset && !probe && !battery) goto invalid_command;
    unsigned slot=0,value=0;
    if (set_dpi && (!cx_number(args[2],0,5,&slot) || !cx_number(args[3],50,6400,&value) || value%50)) goto invalid_command;
    if ((set_rate || live) && (!cx_number(args[2],125,1000,&value) || (value!=125 && value!=500 && value!=1000))) goto invalid_command;
    if (probe && (start>=65536 || length>65536-start)) goto invalid_command;
    if ((set_dpi || set_rate || reset || live) && !confirm) {
        cx_error(&e,CX_REFUSED,"Mutation requires --confirm; use tui for an interactive review"); return fail(cmd,&e,&o,1);
    }
    if (set_rate && !persist) { cx_error(&e,CX_REFUSED,"Stored interval experiment requires --persist and --confirm"); return fail(cmd,&e,&o,1); }
    CxDevice device;
    if (!cx_choose(&o,&device,&e)) return fail(cmd,&e,&o,1);
    CxMutation mutation={0};
    if (reset) {
        (void)cx_reset(&device,o.demo,&mutation,&e); cx_print_mutation(cmd,&mutation,&e,o.json);
        return cx_signal ? 128+cx_signal : e.code!=CX_OK;
    }
    CxSession session;
    if (!cx_open(&session,&device,&o,&e)) return fail(cmd,&e,&o,1);
    if (set_dpi) (void)cx_set_dpi(&session,slot,value,&mutation,&e);
    else if (set_rate) (void)cx_set_interval(&session,value,&mutation,&e);
    else if (live) (void)cx_live_interval(&session,value,&mutation,&e);
    else if (battery) {
        unsigned percent=0,state=0; bool charging=false;
        bool ok=cx_battery(&session,&percent,&charging,&state,&e); cx_close(&session);
        if (!ok) return fail(cmd,&e,&o,1);
        if (o.json) printf("{\"schema\":%d,\"command\":\"battery\",\"battery\":{\"percent\":%u,\"charging\":%s,\"state\":%u}}\n",CX_SCHEMA,percent,charging ? "true" : "false",state);
        else printf("Battery: %u%% (%s), state 0x%02x\n",percent,charging ? "charging" : "not charging",state);
        return 0;
    } else if (probe) {
        uint8_t *data=malloc(length);
        if (!data) { cx_close(&session); cx_error(&e,CX_IO,"Out of memory"); return fail(cmd,&e,&o,1); }
        bool ok=true;
        for (unsigned offset=0;offset<length;offset+=8) {
            unsigned chunk=length-offset<8 ? length-offset : 8;
            if (!cx_read(&session,start+offset,chunk,data+offset,&e)) { ok=false; break; }
        }
        cx_close(&session);
        if (!ok) { free(data); return fail(cmd,&e,&o,1); }
        if (o.json) printf("{\"schema\":%d,\"command\":\"probe\",\"start\":%u,\"length\":%u,\"hex\":\"",CX_SCHEMA,start,length);
        for (unsigned i=0;i<length;++i) {
            if (!o.json && i%8==0) printf("0x%04x: ",start+i);
            printf("%02x%s",data[i],o.json ? "" : i%8==7 || i==length-1 ? "\n" : " ");
        }
        if (o.json) puts("\"}");
        free(data); return 0;
    }
    cx_close(&session);
    cx_print_mutation(cmd,&mutation,&e,o.json);
    return cx_signal ? 128+cx_signal : e.code!=CX_OK;
invalid_command:
    cx_error(&e,CX_REFUSED,"Invalid command or values; run compxctl --help");
    return fail(cmd,&e,&o,2);
argument_error:
    return fail("arguments",&e,&o,2);
}
