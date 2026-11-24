#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* Public field positions verified on the supplied x86-64 build; the controller
 * checks all three hashes. The library additionally checks libavutil's mapped
 * build ID and the process executable before accessing any frame fields. */
static int (*transfer)(void *,const void *,int);
static void *(*frame_alloc)(void);
static void (*frame_free)(void **);
static int (*get_buffer)(void *,int);
static const char *(*format_name)(int);
typedef struct {
    char name[20];
    int planes, chroma_height_divisor, bytes_per_sample, neutral_chroma;
} Format;
/* The controller assigns mask bits in this same order. */
static const Format formats[]={
    {"nv12",2,2,1,128}, {"p010le",2,2,2,32768},
    {"yuv420p",3,2,1,128}, {"yuv422p",3,1,1,128}, {"yuv444p",3,1,1,128},
    {"nv16",2,1,1,128}, {"p016le",2,2,2,32768}, {"p210le",2,1,2,32768},
    {"p216le",2,1,2,32768}, {"yuv420p10le",3,2,2,512},
    {"yuv422p10le",3,1,2,512}, {"yuv444p10le",3,1,2,512},
    {"yuv444p16le",3,1,2,32768}, {"yuva420p",4,2,1,128},
    {"yuv444p10msble",3,1,2,32768}, {"yuv444p12msble",3,1,2,32768},
    {"bgr0",1,1,1,0}, {"rgb0",1,1,1,0}, {"bgra",1,1,1,0}, {"rgba",1,1,1,0}
};
enum { FORMAT_COUNT=sizeof formats/sizeof formats[0] };
static int cuda,vulkan,enabled,format_ids[FORMAT_COUNT];
static unsigned mask;
static pthread_once_t once=PTHREAD_ONCE_INIT;
static _Atomic int reported[2][FORMAT_COUNT];
static _Atomic int skipped[2];
static int number(const void *p,size_t off){int v; memcpy(&v,(const char *)p+off,4);return v;}
static void set_number(void *p,size_t off,int v){memcpy((char *)p+off,&v,4);}
static void *pointer(const void *p,size_t off){void *v;memcpy(&v,(const char *)p+off,sizeof v);return v;}

static void resolve(void){
    transfer=dlvsym(RTLD_NEXT,"av_hwframe_transfer_data","LIBAVUTIL_60");
    if(!transfer) return;
    Dl_info info;
    if(!dladdr((void *)transfer,&info)) return;
    static const unsigned char id[]={0xe0,0xc3,0xfa,0xba,0x7c,0xba,0xd1,0xdf,0x5c,0x15,0x7d,0x5d,0x5d,0x0b,0xd7,0xb8,0x19,0x52,0xae,0xdd};
    /* Only examine the note when the transfer symbol also matches its RVA. */
    if((uintptr_t)transfer-(uintptr_t)info.dli_fbase != TRANSFER_RVA ||
       memcmp((char *)info.dli_fbase+BUILD_ID_RVA,id,sizeof id)) return;
    struct stat app,self;
    const char *test=getenv("FN_BRIDGE_SELFTEST");
    if(!(test && strcmp(test,"pixels")==0) &&
       (stat("/proc/self/exe",&self) || stat("/usr/trim/bin/mediasrv",&app) ||
        self.st_dev!=app.st_dev || self.st_ino!=app.st_ino)) return;
    frame_alloc=dlvsym(RTLD_NEXT,"av_frame_alloc","LIBAVUTIL_60");
    frame_free=dlvsym(RTLD_NEXT,"av_frame_free","LIBAVUTIL_60");
    get_buffer=dlvsym(RTLD_NEXT,"av_frame_get_buffer","LIBAVUTIL_60");
    int (*pix)(const char *)=dlvsym(RTLD_NEXT,"av_get_pix_fmt","LIBAVUTIL_60");
    format_name=dlvsym(RTLD_NEXT,"av_get_pix_fmt_name","LIBAVUTIL_60");
    if(!frame_alloc || !frame_free || !get_buffer || !pix || !format_name) return;
    cuda=pix("cuda"); vulkan=pix("vulkan");
    const char *bits=getenv("FN_BRIDGE_MASK");
    mask=bits?(unsigned)strtoul(bits,NULL,10):3u;
    for(int i=0;i<FORMAT_COUNT;i++) format_ids[i]=pix(formats[i].name);
    enabled=cuda>=0 && vulkan>=0 && format_ids[0]>=0 && format_ids[1]>=0;
}

static void *pool(const void *frame){
    void *ref=pointer(frame,0x148);
    return ref ? pointer(ref,8) : NULL;
}

int fn_bridge_ready(void){
    pthread_once(&once,resolve);
    return enabled;
}

int av_hwframe_transfer_data(void *dst,const void *src,int flags){
    pthread_once(&once,resolve);
    if(!transfer) return -ENOSYS;
    if(!enabled || flags || !src || !dst) return transfer(dst,src,flags);
    int from=number(src,0x74), to=number(dst,0x74);
    if(!((from==cuda && to==vulkan)||(from==vulkan && to==cuda)))
        return transfer(dst,src,flags);
    void *s=pool(src),*d=pool(dst);
    int direction=from==cuda?0:1;
    int format=s?number(s,0x40):-1, out=d?number(d,0x40):-1;
    int index=0;
    while(index<FORMAT_COUNT && format_ids[index]!=format) index++;
    if(format<0 || format!=out || index==FORMAT_COUNT || !(mask & (1u<<index))){
        if(!atomic_exchange(&skipped[direction],1)){
            const char *a=format_name(format), *b=format_name(out);
            fprintf(stderr,"FNBRIDGE3 pid=%ld %s passthrough src_sw=%s dst_sw=%s\n",
                    (long)getpid(),direction?"Vulkan->CUDA":"CUDA->Vulkan",
                    a?a:"unknown",b?b:"unknown");
        }
        return transfer(dst,src,flags);
    }
    int w=number(s,0x44),h=number(s,0x48),dw=number(d,0x44),dh=number(d,0x48);
    int sw=number(src,0x68),sh=number(src,0x6c),ow=number(dst,0x68),oh=number(dst,0x6c);
    if(w<=0 || h<=0 || dw<=0 || dh<=0 || sw<=0 || sh<=0 || ow<=0 || oh<=0 ||
       sw>w || sh>h || ow>dw || oh>dh) return -EINVAL;
    void *tmp=frame_alloc();
    if(!tmp) return -ENOMEM;
    int aw=w>dw?w:dw, ah=h>dh?h:dh;
    set_number(tmp,0x68,aw); set_number(tmp,0x6c,ah); set_number(tmp,0x74,format);
    int rc=get_buffer(tmp,64);
    if(rc>=0){
        const Format *layout=&formats[index];
        memset(pointer(tmp,0),0,(size_t)number(tmp,0x40)*ah);
        size_t div=(size_t)layout->chroma_height_divisor;
        size_t rows=((size_t)ah+div-1)/div;
        for(int plane=1;plane<layout->planes;plane++){
            int stride=number(tmp,0x40+plane*sizeof(int));
            void *data=pointer(tmp,plane*sizeof(void *));
            size_t bytes=(size_t)stride*(plane==3?(size_t)ah:rows);
            if(layout->bytes_per_sample==1) memset(data,plane==3?255:layout->neutral_chroma,bytes);
            else {
                uint16_t *samples=data;
                for(size_t i=0;i<bytes/sizeof(*samples);i++) samples[i]=(uint16_t)layout->neutral_chroma;
            }
        }
        set_number(tmp,0x68,sw); set_number(tmp,0x6c,sh);
        rc=transfer(tmp,src,0);
    }
    if(rc>=0){
        set_number(tmp,0x68,ow); set_number(tmp,0x6c,oh);
        rc=transfer(dst,tmp,0);
    }
    frame_free(&tmp);
    if(!atomic_exchange(&reported[direction][index],1))
        fprintf(stderr,"FNBRIDGE3 pid=%ld %s fmt=%s %dx%d rc=%d\n",(long)getpid(),
                direction?"Vulkan->host->CUDA":"CUDA->host->Vulkan",
                formats[index].name,sw,sh,rc);
    return rc;
}
