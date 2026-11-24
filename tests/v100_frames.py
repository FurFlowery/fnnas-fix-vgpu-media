"""One-shot NV12 pixel comparison for the analyzed fnOS x86-64 build."""
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
P, I, S = C.c_void_p, C.c_int, C.c_char_p
PP = C.POINTER(P)

def bind(lib, name, ret, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = ret, args
    return fn

def pattern(seed, plane):
    # Include row and block position so a swapped UV pair, pitch or tile
    # displacement changes the expected bytes.
    table = bytes((i*73+seed*31+plane*83) % 256 for i in range(256))
    repeat = table*(W//256+2)
    result = []
    for y in range(H if plane == 0 else H//2):
        start = (y*13 + (y//16)*11 + y//256) % 256
        blocks = []
        for x in range(0,W,256):
            j=(start+(x//256)*19)%256
            blocks.append(repeat[j:j+min(256,W-x)])
        result.append(b"".join(blocks))
    return result

def rows(frame, plane):
    data = P.from_address(frame + plane*8).value
    pitch = I.from_address(frame + 0x40 + plane*4).value
    if not data or pitch < W:
        raise RuntimeError("invalid software frame stride")
    return [data+y*pitch for y in range(H if plane==0 else H//2)]

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
            pool=P(ca(d))
            if not pool.value: raise MemoryError("frame pool")
            stack.callback(unref,C.byref(pool))
            fc=P.from_address(pool.value+8).value
            for offset,value in [(0x3c,pix(name)),(0x40,nv),(0x44,W),(0x48,H)]:
                I.from_address(fc+offset).value=value
            ck(ci(pool),"pool "+name.decode())
            pools[name]=pool
            if name==b"vulkan":
                hw=P.from_address(fc+0x18).value
                print(mode,"VkFormats",I.from_address(hw+0x58).value,I.from_address(hw+0x5c).value,flush=True)
        for seed in (1,2,3):
            with ExitStack() as frames:
                def new(kind):
                    p=P(fa())
                    if not p.value: raise MemoryError("frame")
                    frames.callback(ff,C.byref(p))
                    if kind:
                        ck(alloc(pools[kind],p,0),"allocate "+kind.decode())
                    else:
                        for offset,value in [(0x68,W),(0x6c,H),(0x74,nv)]:
                            I.from_address(p.value+offset).value=value
                        ck(fb(p,32),"allocate NV12")
                    return p.value
                expected=[pattern(seed,plane) for plane in (0,1)]
                src=new(None)
                for plane in (0,1):
                    for addr,line in zip(rows(src,plane),expected[plane]):
                        C.memmove(addr,line,W)
                def copy(kind,source,label):
                    out=new(kind)
                    ck(move(out,source,0),label)
                    return out
                def check(source,label):
                    nonlocal bad
                    out=copy(None,source,label+" readback")
                    diffs=[]
                    for plane in (0,1):
                        mismatch=sum(C.string_at(a,W)!=r for a,r in zip(rows(out,plane),expected[plane]))
                        diffs.append(mismatch)
                    bad |= any(diffs)
                    print(mode,seed,label,"DIFF" if any(diffs) else "OK","Y_rows=%d UV_rows=%d"%tuple(diffs),flush=True)
                cu=copy(b"cuda",src,"CPU->CUDA")
                check(cu,"CUDA_control")
                for source,forward,reverse in [(src,"VK_control","VK_to_CUDA"),
                                               (cu,"CUDA_to_VK","round_trip")]:
                    try:
                        vk=copy(b"vulkan",source,forward+" upload")
                    except RuntimeError as exc:
                        print("ERROR",mode,exc,flush=True)
                        bad=True
                        continue
                    for label in (forward,reverse):
                        try:
                            frame=vk if label==forward else copy(b"cuda",vk,reverse+" transfer")
                            check(frame,label)
                        except RuntimeError as exc:
                            print("ERROR",mode,exc,flush=True)
                            bad=True
    return int(bad)

def main():
    if sys.platform!="linux": sys.exit("Run on the NAS.")
    if len(sys.argv)==3 and sys.argv[1]=="--case" and sys.argv[2] in ("default","split"):
        return child(sys.argv[2])
    if len(sys.argv)!=1: sys.exit("Usage: python3 v100_frames.py")
    status=0
    for mode in ("split","default"):
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
