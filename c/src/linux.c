#define _XOPEN_SOURCE 700
#include "compx.h"

#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <limits.h>
#include <linux/hidraw.h>
#include <linux/usbdevice_fs.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static bool path_join(char out[CX_PATH], const char *a, const char *b) {
    int n = snprintf(out,CX_PATH,"%s/%s",a,b);
    return n >= 0 && n < CX_PATH;
}

static bool text(const char *path, char *out, size_t size) {
    if (size < 2) return false;
    FILE *f = fopen(path,"re");
    if (!f) return false;
    bool ok = fgets(out,(int)size,f) != NULL;
    fclose(f);
    if (ok) out[strcspn(out,"\r\n")] = 0;
    return ok;
}

static bool attribute(const char *dir, const char *key, unsigned base, unsigned *value) {
    char path[CX_PATH], buf[128], *end;
    if (!path_join(path,dir,key) || !text(path,buf,sizeof(buf)) || !buf[0] || buf[0]=='-') return false;
    errno=0;
    unsigned long n = strtoul(buf,&end,(int)base);
    if (errno || *end || n>UINT_MAX) return false;
    *value=(unsigned)n;
    return true;
}

static bool supported(unsigned product) {
    return product==0xfa7b || product==0xfa7c || product==0xfa03 || product==0xfa93;
}

static bool config_descriptor(const char *hid) {
    char path[CX_PATH];
    if (!path_join(path,hid,"report_descriptor")) return false;
    int fd=open(path,O_RDONLY|O_CLOEXEC);
    if (fd<0) return false;
    uint8_t bytes[HID_MAX_DESCRIPTOR_SIZE];
    ssize_t n=read(fd,bytes,sizeof(bytes));
    close(fd);
    CxDescriptor d; CxError e={0};
    return n>0 && cx_descriptor(bytes,(size_t)n,&d,&e) && d.feature8 && d.input9;
}

static void discover_node(CxDevice *d) {
    glob_t nodes={0};
    if (glob("/sys/class/hidraw/hidraw*/device",0,NULL,&nodes)!=0) { globfree(&nodes); return; }
    char device_real[CX_PATH];
    if (!realpath(d->usb_dir,device_real)) { globfree(&nodes); return; }
    size_t prefix=strlen(device_real);
    for (size_t i=0;i<nodes.gl_pathc;++i) {
        char hid[CX_PATH];
        if (!realpath(nodes.gl_pathv[i],hid) || strncmp(hid,device_real,prefix) || hid[prefix]!='/') continue;
        if (!config_descriptor(hid)) continue;
        char *name=strstr(nodes.gl_pathv[i],"/hidraw/hidraw");
        if (!name) continue;
        name+=8;
        size_t length=strcspn(name,"/");
        if (length>32) continue;
        (void)snprintf(d->node,sizeof(d->node),"/dev/%.*s",(int)length,name);
        break;
    }
    globfree(&nodes);
}

static void discover_endpoint(CxDevice *d) {
    char pattern[CX_PATH];
    if (snprintf(pattern,sizeof(pattern),"%s:*",d->usb_dir)>=(int)sizeof(pattern)) return;
    glob_t interfaces={0};
    if (glob(pattern,0,NULL,&interfaces)!=0) { globfree(&interfaces); return; }
    for (size_t i=0;i<interfaces.gl_pathc;++i) {
        unsigned cls, sub, proto;
        const char *dir=interfaces.gl_pathv[i];
        if (!attribute(dir,"bInterfaceClass",16,&cls) || cls!=3 ||
            !attribute(dir,"bInterfaceSubClass",16,&sub) || sub!=1 ||
            !attribute(dir,"bInterfaceProtocol",16,&proto) || proto!=2) continue;
        if (!path_join(pattern,dir,"ep_*")) continue;
        glob_t endpoints={0};
        if (glob(pattern,0,NULL,&endpoints)!=0) { globfree(&endpoints); continue; }
        for (size_t j=0;j<endpoints.gl_pathc;++j) {
            unsigned attrs,address;
            const char *ep=endpoints.gl_pathv[j];
            if (!attribute(ep,"bmAttributes",16,&attrs) || (attrs&3)!=3 ||
                !attribute(ep,"bEndpointAddress",16,&address) || !(address&128)) continue;
            char path[CX_PATH],value[128],unit[8]={0}; unsigned interval;
            if (path_join(path,ep,"interval") && text(path,value,sizeof(value)) &&
                sscanf(value,"%u%7s",&interval,unit)==2 && interval>0) {
                if (!strcmp(unit,"ms")) d->advertised_ms=interval;
                else if (!strcmp(unit,"us")) d->advertised_ms=interval/1000.0;
                d->advertised_known=d->advertised_ms>0;
            }
            break;
        }
        globfree(&endpoints);
        if (d->advertised_known) break;
    }
    globfree(&interfaces);
}

bool cx_discover(CxDevice *out,size_t capacity,size_t *count,CxError *e) {
    *count=0;
    glob_t devices={0};
    int rc=glob("/sys/bus/usb/devices/*",0,NULL,&devices);
    if (rc==GLOB_NOMATCH) { globfree(&devices); return true; }
    if (rc!=0) { globfree(&devices); cx_error(e,CX_IO,"Cannot enumerate USB sysfs"); return false; }
    for (size_t i=0;i<devices.gl_pathc;++i) {
        unsigned vid,pid;
        const char *dir=devices.gl_pathv[i];
        if (!attribute(dir,"idVendor",16,&vid) || vid!=0x25a7 ||
            !attribute(dir,"idProduct",16,&pid) || !supported(pid)) continue;
        if (*count==capacity) { globfree(&devices); cx_error(e,CX_REFUSED,"Too many devices; discovery limit exceeded"); return false; }
        CxDevice *d=&out[(*count)++];
        memset(d,0,sizeof(*d));
        const char *id=strrchr(dir,'/'); id=id ? id+1 : dir;
        if (strlen(id)>=sizeof(d->id) || strlen(dir)>=sizeof(d->usb_dir)) {
            globfree(&devices); cx_error(e,CX_REFUSED,"USB sysfs path too long"); return false;
        }
        strcpy(d->id,id); strcpy(d->usb_dir,dir); d->product=pid;
        char path[CX_PATH];
        if (path_join(path,dir,"product")) (void)text(path,d->name,sizeof(d->name));
        if (!d->name[0]) strcpy(d->name,"CompX USB device");
        /* USB names are untrusted terminal input; strip control/escape bytes. */
        for (size_t j=0;d->name[j];++j) if ((unsigned char)d->name[j]<32 || (unsigned char)d->name[j]==127) d->name[j]='?';
        discover_node(d); discover_endpoint(d);
    }
    globfree(&devices);
    return true;
}

bool cx_choose(const CxOptions *o,CxDevice *d,CxError *e) {
    if (o->demo) {
        memset(d,0,sizeof(*d));
        strcpy(d->id,"demo"); strcpy(d->name,"Ardor Gaming Ulta / SIMULATED");
        strcpy(d->node,"demo://memory"); d->product=0xfa7b;
        d->advertised_known=true; d->advertised_ms=1;
        return true;
    }
    CxDevice devices[CX_MAX_DEVICES]; size_t n;
    if (!cx_discover(devices,CX_MAX_DEVICES,&n,e)) return false;
    if (o->device_id[0]) {
        for (size_t i=0;i<n;++i) if (!strcmp(o->device_id,devices[i].id)) { *d=devices[i]; return true; }
        cx_error(e,CX_REFUSED,"Selected device '%s' is not connected",o->device_id);
        return false;
    }
    if (n!=1) {
        cx_error(e,CX_REFUSED,n ? "Multiple mice found; use --device USB-ID (see devices)" :
                 "No supported mouse found (VID 25a7); connect its USB receiver");
        return false;
    }
    *d=devices[0]; return true;
}

int cx_receive_fd(void *context,uint8_t *buf,size_t capacity,unsigned timeout,CxError *e) {
    int fd=*(int *)context;
    int64_t deadline=cx_now_ms()+timeout;
    for (;;) {
        if (cx_signal) { cx_error(e,CX_INTERRUPTED,"Interrupted"); return -1; }
        int64_t left=deadline-cx_now_ms();
        int wait=left>0 ? (left>INT_MAX ? INT_MAX : (int)left) : 0;
        struct pollfd p={.fd=fd,.events=POLLIN};
        int rc=poll(&p,1,wait);
        if (rc<0) { if (errno==EINTR) continue; cx_error(e,CX_IO,"poll: %s",strerror(errno)); return -1; }
        if (!rc) return 0;
        if (p.revents&(POLLERR|POLLHUP|POLLNVAL)) { cx_error(e,CX_IO,"Device disconnected or descriptor invalid"); return -1; }
        if (p.revents&POLLIN) {
            ssize_t n=read(fd,buf,capacity);
            if (n>0) return (int)n;
            if (!n) { cx_error(e,CX_IO,"Device stream closed"); return -1; }
            if (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) {
                cx_error(e,CX_IO,"read: %s",strerror(errno)); return -1;
            }
        }
        if (cx_now_ms()>=deadline) return 0;
    }
}

static bool send_fd(void *context,const uint8_t *data,size_t n,CxError *e) {
    if (cx_signal) { cx_error(e,CX_INTERRUPTED,"Interrupted before send"); return false; }
    if (n!=8 && n!=CX_FRAME) { cx_error(e,CX_REFUSED,"Invalid feature report size"); return false; }
    uint8_t buf[CX_FRAME]; memcpy(buf,data,n);
    int fd=*(int *)context;
    int rc=ioctl(fd,HIDIOCSFEATURE(n),buf);
    if (rc<0 || (size_t)rc!=n) {
        cx_error(e,cx_signal ? CX_INTERRUPTED : CX_IO,"SET_REPORT failed or short; delivery may be unknown: %s",
                 rc<0 ? strerror(errno) : "short write"); return false;
    }
    return true;
}

/* In-process simulator: all writes are volatile and never touch sysfs/hidraw. */
static uint8_t demo_memory[65536], demo_queue[CX_FRAME];
static bool demo_ready, demo_initialized;
void cx_demo_reset(void) {
    memset(demo_memory,0,sizeof(demo_memory));
    demo_memory[0]=1; demo_memory[1]=0x54; demo_memory[4]=1; demo_memory[5]=0x54;
    unsigned values[8]={400,800,1600,2400,3200,6400,0,0};
    CxError e={0};
    for (unsigned i=0;i<6;++i) (void)cx_dpi_payload(values[i],demo_memory+12+4*i,&e);
    memset(demo_memory+36,255,8);
    demo_ready=false; demo_initialized=true;
}
static bool demo_send(void *ctx,const uint8_t *p,size_t n,CxError *e) {
    (void)ctx;
    if (n==8 || (n==17 && p[1]==0x11)) return true;
    if (n!=17) { cx_error(e,CX_PROTOCOL,"Bad demo packet"); return false; }
    unsigned addr=((unsigned)p[3]<<8)|p[4],len=p[5];
    if (p[1]==7 || p[1]==8) {
        if (!len || len>10 || len>65536-addr) { cx_error(e,CX_PROTOCOL,"Bad demo window"); return false; }
        if (p[1]==7) { memcpy(demo_memory+addr,p+6,len); return true; }
    }
    memset(demo_queue,0,sizeof(demo_queue)); demo_queue[0]=9;
    if (p[1]==4) { demo_queue[1]=4; demo_queue[5]=2; demo_queue[6]=87; }
    else { memcpy(demo_queue+1,p+1,5); memcpy(demo_queue+6,demo_memory+addr,len); }
    demo_queue[16]=cx_checksum(demo_queue,16); demo_ready=true; return true;
}
static int demo_receive(void *ctx,uint8_t *out,size_t cap,unsigned timeout,CxError *e) {
    (void)ctx; (void)timeout; (void)e;
    if (!demo_ready || cap<CX_FRAME) return 0;
    memcpy(out,demo_queue,CX_FRAME); demo_ready=false; return CX_FRAME;
}

void cx_close(CxSession *s) {
    if (s->fd>=0) { close(s->fd); s->fd=-1; }
    if (s->lock_fd>=0) { close(s->lock_fd); s->lock_fd=-1; }
}

bool cx_open(CxSession *s,const CxDevice *d,const CxOptions *o,CxError *e) {
    memset(s,0,sizeof(*s)); s->fd=s->lock_fd=-1;
    s->device=*d; s->timeout_ms=o->timeout_ms; s->tries=o->tries; s->demo=o->demo;
    if (s->demo) {
        if (!demo_initialized) cx_demo_reset();
        demo_ready=false;
        s->transport=(CxTransport){NULL,demo_receive,demo_send}; return true;
    }
    if (!d->node[0]) { cx_error(e,CX_REFUSED,"No verified config HID interface; inspect permissions/report descriptor"); return false; }
    s->lock_fd=open(d->node,O_RDWR|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
    if (s->lock_fd<0) goto io_error;
    int64_t deadline=cx_now_ms()+2000;
    while (flock(s->lock_fd,LOCK_EX|LOCK_NB)!=0) {
        if (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EACCES) goto io_error;
        if (cx_signal) { cx_error(e,CX_INTERRUPTED,"Interrupted waiting for device lock"); goto fail; }
        if (cx_now_ms()>=deadline) { cx_error(e,CX_TIMEOUT,"Device busy: another compxctl holds its lock"); goto fail; }
        struct timespec pause={0,20000000}; (void)nanosleep(&pause,NULL);
    }
    s->fd=open(d->node,O_RDWR|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
    if (s->fd<0) goto io_error;
    struct stat a,b;
    struct hidraw_devinfo info={0};
    if (fstat(s->fd,&a)!=0 || fstat(s->lock_fd,&b)!=0 || ioctl(s->fd,HIDIOCGRAWINFO,&info)<0) goto io_error;
    if (!S_ISCHR(a.st_mode) || a.st_rdev!=b.st_rdev || a.st_ino!=b.st_ino ||
        (uint16_t)info.vendor!=0x25a7 || (uint16_t)info.product!=d->product) {
        cx_error(e,CX_REFUSED,"Device identity changed during open"); goto fail;
    }
    int length=0;
    struct hidraw_report_descriptor desc={0};
    if (ioctl(s->fd,HIDIOCGRDESCSIZE,&length)<0) goto io_error;
    if (length<1 || length>HID_MAX_DESCRIPTOR_SIZE) { cx_error(e,CX_PROTOCOL,"Invalid descriptor length"); goto fail; }
    desc.size=(uint32_t)length;
    if (ioctl(s->fd,HIDIOCGRDESC,&desc)<0) goto io_error;
    CxDescriptor parsed;
    if (!cx_descriptor(desc.value,desc.size,&parsed,e)) goto fail;
    if (!parsed.feature8 || !parsed.input9) {
        cx_error(e,CX_REFUSED,"Config interface must declare 16-byte feature 8 and input 9 reports"); goto fail;
    }
    s->transport=(CxTransport){&s->fd,cx_receive_fd,send_fd};
    return true;
io_error:
    cx_error(e,CX_IO,"Open/inspect %s: %s (install 70-compx-mouse.rules for desktop access)",d->node,strerror(errno));
fail:
    cx_close(s); return false;
}

bool cx_host_rate(unsigned rate,bool demo,CxMutation *m,CxError *e) {
    memset(m,0,sizeof(*m));
    unsigned interval;
    if (rate==0) interval=0;
    else if (rate==125) interval=8;
    else if (rate==500) interval=2;
    else if (rate==1000) interval=1;
    else { cx_error(e,CX_REFUSED,"Host rate must be 0 (descriptor), 125, 500 or 1000"); return false; }
    if (demo) { snprintf(m->description,sizeof(m->description),"SIMULATED host override %u; no system write",interval); return true; }
    unsigned before;
    const char *path="/sys/module/usbhid/parameters/mousepoll";
    if (!attribute("/sys/module/usbhid/parameters","mousepoll",10,&before)) {
        cx_error(e,CX_IO,"Cannot read usbhid.mousepoll; host parameter unavailable"); return false;
    }
    if (before!=interval) {
        int fd=open(path,O_WRONLY|O_CLOEXEC);
        if (fd<0) { cx_error(e,CX_IO,"Open mousepoll: %s; host changes require root",strerror(errno)); return false; }
        char buf[16]; int n=snprintf(buf,sizeof(buf),"%u\n",interval);
        m->write_attempted=true;
        ssize_t sent=write(fd,buf,(size_t)n); int saved=errno; close(fd);
        if (sent!=n) { cx_error(e,CX_IO,"Host parameter write uncertain: %s",strerror(saved)); return false; }
        m->changed=true;
    }
    unsigned after;
    if (!attribute("/sys/module/usbhid/parameters","mousepoll",10,&after) || after!=interval) {
        cx_error(e,CX_IO,"Host parameter readback failed; inspect current setting"); return false;
    }
    m->readback_verified=true;
    snprintf(m->description,sizeof(m->description),
             "Host parameter = %u. Global setting; reconnect affected mice to apply. Actual polling unmeasured.",interval);
    return true;
}

bool cx_reset(const CxDevice *d,bool demo,CxMutation *m,CxError *e) {
    memset(m,0,sizeof(*m));
    if (demo) { strcpy(m->description,"SIMULATED reset; no USB operation"); return true; }
    unsigned bus,dev;
    if (!attribute(d->usb_dir,"busnum",10,&bus) || !attribute(d->usb_dir,"devnum",10,&dev)) {
        cx_error(e,CX_IO,"Device disappeared before reset"); return false;
    }
    char node[64]; snprintf(node,sizeof(node),"/dev/bus/usb/%03u/%03u",bus,dev);
    int lock=open(d->node,O_RDWR|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
    if (lock<0 || flock(lock,LOCK_EX|LOCK_NB)!=0) {
        if (lock>=0) close(lock);
        cx_error(e,CX_IO,"Cannot lock device for reset: %s",strerror(errno)); return false;
    }
    int fd=open(node,O_RDWR|O_CLOEXEC|O_NOFOLLOW);
    if (fd<0) { close(lock); cx_error(e,CX_IO,"Open USB reset node: %s; optional USB rule required",strerror(errno)); return false; }
    /* usbfs exposes descriptors on read. Validate the opened node, not just a
     * potentially recycled bus/device number obtained earlier from sysfs. */
    uint8_t descriptor[18];
    ssize_t got=read(fd,descriptor,sizeof(descriptor));
    unsigned vid=got==18 ? (unsigned)descriptor[8]|((unsigned)descriptor[9]<<8) : 0;
    unsigned pid=got==18 ? (unsigned)descriptor[10]|((unsigned)descriptor[11]<<8) : 0;
    if (got!=18 || descriptor[0]!=18 || descriptor[1]!=1 || vid!=0x25a7 || pid!=d->product) {
        close(fd); close(lock); cx_error(e,CX_REFUSED,"USB identity changed or cannot be verified; reset refused"); return false;
    }
    if (cx_signal) { close(fd); close(lock); cx_error(e,CX_INTERRUPTED,"Interrupted before USB reset"); return false; }
    m->write_attempted=true;
    int rc=ioctl(fd,USBDEVFS_RESET,0), saved=errno;
    close(fd); close(lock);
    if (rc<0) { cx_error(e,CX_IO,"USB reset failed; device state unknown: %s",strerror(saved)); return false; }
    m->changed=true;
    snprintf(m->description,sizeof(m->description),"USB reset accepted. All receiver functions were affected; reconnect/refresh to verify device availability.");
    return true;
}
