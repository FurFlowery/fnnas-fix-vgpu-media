"""NV12 host-staged CUDA/Vulkan scaling test for the analyzed fnOS x86-64 build."""
import ctypes as C
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys

LIB = Path("/usr/trim/lib/mediasrv/lib/libavutil.so.60")
HASH = "09f6706ca66afa479553cf0957eded05d194456dd9c8badb130610162fc1770c"
W, H = 1920, 1088
OW, OH = 960, 544
FILTER = LIB.with_name("libavfilter.so.11")
FILTER_SHA = "21ab53dc2e1d6a0cb560998fac778fd20611b5a0159a0a55c67b1a0496e65015"
P, I, S = C.c_void_p, C.c_int, C.c_char_p
PP = C.POINTER(P)

def bind(lib, name, ret, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = ret, args
    return fn

def pattern(seed, plane, w, h):
    result=[]
    for y in range(h if plane==0 else h//2):
        # Tile edges align on luma and chroma sample boundaries at both sizes.
        by=y//((16 if plane else 32)*w//W)
        row=bytearray()
        for x in range(0,w,32*w//W):
            bx=x//(32*w//W)
            t=(bx*13 + by*31 + seed*19)%173
            pixel=bytes((36+t,)) if plane==0 else bytes((36+t,214-t))
            row.extend(pixel * (32*w//W//len(pixel)))
        result.append(bytes(row))
    return result

def rows(frame, plane,w,h):
    data = P.from_address(frame + plane*8).value
    pitch = I.from_address(frame + 0x40 + plane*4).value
    if not data or pitch < w:
        raise RuntimeError("invalid software frame stride")
    return [data+y*pitch for y in range(h if plane==0 else h//2)]

def make_scaler(vlib, util, vkpool, pixfmt, ck, lifetime):
    graph=P(bind(vlib,"avfilter_graph_alloc",P)())
    if not graph.value: raise MemoryError("graph")
    lifetime.callback(bind(vlib,"avfilter_graph_free",None,PP),C.byref(graph))
    lookup=bind(vlib,"avfilter_get_by_name",P,S)
    make=bind(vlib,"avfilter_graph_alloc_filter",P,P,P,S)
    init=bind(vlib,"avfilter_init_str",I,P,S)
    link=bind(vlib,"avfilter_link",I,P,C.c_uint,P,C.c_uint)
    nodes=[]
    for name in (b"buffer",b"scale_vulkan",b"buffersink"):
        desc=lookup(name)
        if not desc: raise RuntimeError("missing filter "+name.decode())
        node=make(graph,desc,name)
        if not node: raise MemoryError("filter context")
        nodes.append(node)
    source,scale,sink=nodes
    params=bind(vlib,"av_buffersrc_parameters_alloc",P)()
    if not params: raise MemoryError("buffer source params")
    try:
        I.from_address(params).value=pixfmt
        P.from_address(params+0x28).value=vkpool.value
        ck(bind(vlib,"av_buffersrc_parameters_set",I,P,P)(source,params),"buffer source hardware context")
    finally:
        bind(util,"av_free",None,P)(params)
    ck(init(source,("video_size=%dx%d:pix_fmt=vulkan:time_base=1/25:pixel_aspect=1/1"%(W,H)).encode()),"buffer init")
    ck(init(scale,("w=%d:h=%d:format=nv12:scaler=nearest:transpose=none"%(OW,OH)).encode()),"scale_vulkan init")
    ck(init(sink,None),"sink init")
    ck(link(source,0,scale,0),"input link")
    ck(link(scale,0,sink,0),"output link")
    ck(bind(vlib,"avfilter_graph_config",I,P,P)(graph,None),"graph config")
    push=bind(vlib,"av_buffersrc_add_frame_flags",I,P,P,I)
    pull=bind(vlib,"av_buffersink_get_frame",I,P,P)
    def process(vk,out):
        ck(push(source,vk,8),"push Vulkan frame")  # KEEP_REF
        ck(pull(sink,out),"pull scaled frame")
        if (I.from_address(out+0x68).value,I.from_address(out+0x6c).value,I.from_address(out+0x74).value)!=(OW,OH,pixfmt):
            raise RuntimeError("unexpected scale output geometry or hardware format")
    return process

def child(mode):
    if platform.machine().lower() not in ("x86_64","amd64") or C.sizeof(P)!=8:
        raise RuntimeError("requires x86-64")
    # Hash the same inode opened by dlopen, before interpreting ABI fields.
    with LIB.open("rb") as src:
        if hashlib.sha256(src.read()).hexdigest()!=HASH:
            raise RuntimeError("original libavutil SHA-256 mismatch")
        lib=C.CDLL(str(LIB))
        st=os.fstat(src.fileno())
        if not any(len(q)>=5 and "x" in q[1] and int(q[4])==st.st_ino and
                   tuple(int(i,16) for i in q[3].split(":"))==(os.major(st.st_dev),os.minor(st.st_dev))
                   for q in (line.split(None,5) for line in Path("/proc/self/maps").read_text(errors="replace").splitlines())):
            raise RuntimeError("loaded library inode mismatch")
    fa=bind(lib,"av_frame_alloc",P)
    ff=bind(lib,"av_frame_free",None,PP)
    fb=bind(lib,"av_frame_get_buffer",I,P,I)
    dc=bind(lib,"av_hwdevice_ctx_create",I,PP,I,S,P,I)
    dt=bind(lib,"av_hwdevice_find_type_by_name",I,S)
    ds=bind(lib,"av_dict_set",I,PP,S,S,I)
    df=bind(lib,"av_dict_free",None,PP)
    ca=bind(lib,"av_hwframe_ctx_alloc",P,P)
    ci=bind(lib,"av_hwframe_ctx_init",I,P)
    alloc=bind(lib,"av_hwframe_get_buffer",I,P,P,I)
    move=bind(lib,"av_hwframe_transfer_data",I,P,P,I)
    unref=bind(lib,"av_buffer_unref",None,PP)
    pix=bind(lib,"av_get_pix_fmt",I,S)
    errstr=bind(lib,"av_strerror",I,I,P,C.c_size_t)
    def ck(value,step):
        if value<0:
            msg=C.create_string_buffer(128)
            errstr(value,msg,len(msg))
            raise RuntimeError("%s: %s (%d)"%(step,msg.value.decode(errors="replace"),value))
    nv=pix(b"nv12")
    if hashlib.sha256(FILTER.read_bytes()).hexdigest()!=FILTER_SHA:
        raise RuntimeError("libavfilter SHA-256 mismatch")
    vlib=C.CDLL(str(FILTER))
    bad=False
    with ExitStack() as stack:
        pools={}
        for name in (b"cuda",b"vulkan"):
            d,opt=P(),P()
            stack.callback(df,C.byref(opt))
            if name==b"vulkan" and mode=="split":
                ck(ds(C.byref(opt),b"disable_multiplane",b"1",0),"option")
            ck(dc(C.byref(d),dt(name),None,opt,0),"device "+name.decode())
            stack.callback(unref,C.byref(d))
            for w,h in ([(W,H),(OW,OH)] if name==b"cuda" else [(W,H)]):
                pool=P(ca(d))
                if not pool.value: raise MemoryError("frame pool")
                stack.callback(unref,C.byref(pool))
                fc=P.from_address(pool.value+8).value
                for offset,value in [(0x3c,pix(name)),(0x40,nv),(0x44,w),(0x48,h)]:
                    I.from_address(fc+offset).value=value
                ck(ci(pool),"pool "+name.decode())
                pools[name,w,h]=pool
        scale=make_scaler(vlib,lib,pools[b"vulkan",W,H],pix(b"vulkan"),ck,stack)
        for seed in (1,2,3):
            with ExitStack() as frames:
                def new(kind,w,h):
                    p=P(fa())
                    if not p.value: raise MemoryError("frame")
                    frames.callback(ff,C.byref(p))
                    if kind==b"host":
                        for offset,value in [(0x68,w),(0x6c,h),(0x74,nv)]:
                            I.from_address(p.value+offset).value=value
                        ck(fb(p,32),"allocate NV12")
                    elif kind:
                        ck(alloc(pools[kind,w,h],p,0),"allocate "+kind.decode())
                    return p.value
                def copy(kind,source,w,h,label):
                    out=new(kind,w,h)
                    ck(move(out,source,0),label)
                    return out
                def compare(label,host,w,h):
                    nonlocal bad
                    counts=[]
                    for plane in (0,1):
                        expected=pattern(seed,plane,w,h)
                        counts.append(sum(C.string_at(a,w)!=r for a,r in zip(rows(host,plane,w,h),expected)))
                    bad |= any(counts)
                    print(mode,seed,label,"DIFF" if any(counts) else "OK","Y_rows=%d UV_rows=%d"%tuple(counts),flush=True)
                src=new(b"host",W,H)
                for plane in (0,1):
                    for addr,line in zip(rows(src,plane,W,H),pattern(seed,plane,W,H)):
                        C.memmove(addr,line,W)
                cu=copy(b"cuda",src,W,H,"CPU to CUDA")
                cpu=copy(b"host",cu,W,H,"CUDA to CPU")
                compare("cuda_download",cpu,W,H)
                vk=copy(b"vulkan",cpu,W,H,"CPU to Vulkan")
                compare("staged_input",copy(b"host",vk,W,H,"input readback"),W,H)
                scaled=new(None,OW,OH)
                scale(vk,scaled)
                small=copy(b"host",scaled,OW,OH,"scaled Vulkan to CPU")
                compare("scale_readback",small,OW,OH)
                cu_out=copy(b"cuda",small,OW,OH,"CPU to output CUDA")
                compare("staged_output",copy(b"host",cu_out,OW,OH,"CUDA readback"),OW,OH)
    return int(bad)

def main():
    if sys.platform!="linux": sys.exit("Run on the NAS.")
    if len(sys.argv)==3 and sys.argv[1]=="--case" and sys.argv[2] in ("default","split"):
        return child(sys.argv[2])
    if len(sys.argv)!=1: sys.exit("Usage: python3 v100_host_scale.py")
    status=0
    for mode in ("split",):
        try:
            r=subprocess.run([sys.executable,str(Path(__file__).resolve()),"--case",mode],timeout=45)
            print("CASE",mode,"exit",r.returncode,flush=True)
            status |= int(r.returncode!=0)
        except subprocess.TimeoutExpired:
            print("CASE",mode,"TIMEOUT",flush=True)
            status=1
    return status

if __name__=="__main__":
    try: sys.exit(main())
    except (OSError,RuntimeError,MemoryError) as exc:
        print("ERROR",exc,flush=True)
        sys.exit(2)
