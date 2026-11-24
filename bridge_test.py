"""Test the 20 software frame formats in the fnOS 0.8.45 CUDA format table."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tempfile
from pixels import LAYOUTS

UNIT="mediasrv.service"
HERE=Path(__file__).resolve().parent
STAGE=Path("/var/lib/fn-v100-host-bridge-v3")
SO=STAGE/"libfnbridge.so"
CONF=Path("/run/systemd/system/mediasrv.service.d/97-fn-v100-host-bridge-v3.conf")
CONFIG="[Service]\nEnvironment=LD_PRELOAD="+str(SO)+"\n"
OLD=CONF.with_name("94-fn-v100-multiplane-test.conf")
V1_CONF=CONF.with_name("95-fn-v100-host-bridge.conf")
V1_SO=Path("/var/lib/fn-v100-host-bridge/libfnbridge.so")
V1_HASH="b641fa3f5eb3262d2993c933f6ef7ed6d0665ceb3a2a49f9815641d745bef5ca"
V2_CONF=CONF.with_name("96-fn-v100-host-bridge-v2.conf")
V2_SO=Path("/var/lib/fn-v100-host-bridge-v2/libfnbridge.so")
V2_HASH="fdd9b043a1cd1e52b3ab9c71d6b966f43a77c3c09e56ea0bd70cce7763c34509"
FORMATS=tuple(LAYOUTS)
LIB=Path("/usr/trim/lib/mediasrv/lib/libavutil.so.60")
FILTER=LIB.with_name("libavfilter.so.11")
EXE=Path("/usr/trim/bin/mediasrv")
EXPECTED={EXE:"e3c381af4db916e4b29f1ee9c7a2c64afaa6aeff3cc14bbd0969b8597315ac35",
          LIB:"09f6706ca66afa479553cf0957eded05d194456dd9c8badb130610162fc1770c",
          FILTER:"21ab53dc2e1d6a0cb560998fac778fd20611b5a0159a0a55c67b1a0496e65015"}
SO_HASH="53f018e0329afb9e3342fddb60a5d550d023619f138a80360cc5ff9d78e8bf75"
# The field-tested preview has the same mapped ELF sections and includes DWARF.
PREVIEW_SO_HASH="01e6e42c5a4586343525c0ad376c32017a3e99ceab0b6b9abb0620d0efed82b6"

def config(mask):
    return CONFIG+f"Environment=FN_BRIDGE_MASK={mask}\n"

def installed_mask():
    if CONF.is_symlink(): raise RuntimeError("配置路径异常")
    if not CONF.exists(): return None
    match=re.fullmatch(re.escape(CONFIG)+r"Environment=FN_BRIDGE_MASK=([0-9]+)\n",CONF.read_text())
    if not match: raise RuntimeError("同名配置内容不符")
    mask=int(match.group(1))
    if (mask&3)!=3 or mask >= 1<<len(FORMATS): raise RuntimeError("配置格式列表异常")
    return mask

def format_names(mask):
    return ", ".join(fmt for i,fmt in enumerate(FORMATS) if mask & (1<<i))
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def ctl(*args):
    return subprocess.check_output(["systemctl",*args],universal_newlines=True).strip()
def current_pid(): return int(ctl("show","--value","-p","MainPID",UNIT))
def live(need_bridge,verify_mask=True):
    pid=current_pid()
    if not pid: return False
    proc=Path("/proc")/str(pid)
    try:
        if not os.path.samestat((proc/"exe").stat(),EXE.stat()): return False
        maps=set()
        for line in (proc/"maps").read_bytes().splitlines():
            q=line.split(None,5)
            if b"x" in q[1]:
                maps.add((*[int(n,16) for n in q[3].split(b":")],int(q[4])))
        def loaded(p):
            s=p.stat()
            return (os.major(s.st_dev),os.minor(s.st_dev),s.st_ino) in maps
        want_v2=V2_CONF.exists() and not need_bridge
        want_v1=V1_CONF.exists() and not (want_v2 or need_bridge)
        if need_bridge and verify_mask:
            mask=installed_mask()
            env=(proc/"environ").read_bytes().split(b"\0")
            if mask is None or f"FN_BRIDGE_MASK={mask}".encode() not in env: return False
        return (loaded(LIB) and loaded(FILTER)
                and (SO.exists() and loaded(SO))==need_bridge
                and (V1_SO.exists() and loaded(V1_SO))==want_v1
                and (V2_SO.exists() and loaded(V2_SO))==want_v2)
    except FileNotFoundError: return False
def restart(need_bridge):
    ctl("daemon-reload")
    ctl("restart",UNIT)
    for _ in range(40):
        if live(need_bridge): return
        time.sleep(.5)
    raise RuntimeError("重启后未核实预期库映射。")
def stock():
    if OLD.exists(): raise RuntimeError("仍有上一轮 multiplane 配置，请用原试用包 rollback。")
    for path,want in EXPECTED.items():
        if digest(path)!=want: raise RuntimeError("版本不匹配 "+str(path))
    if not live(False): raise RuntimeError("服务未运行原版样本，或中转已经启用。")
    environ=(Path("/proc")/str(current_pid())/"environ").read_bytes().split(b"\0")
    allowed={b"LD_PRELOAD="}
    for conf,lib,want in [(V1_CONF,V1_SO,V1_HASH),(V2_CONF,V2_SO,V2_HASH)]:
        if conf.exists():
            if (conf.read_text()!="[Service]\nEnvironment=LD_PRELOAD="+str(lib)+"\n"
                    or digest(lib)!=want):
                raise RuntimeError("现有补丁与实测版本不符 "+str(conf))
            allowed={b"LD_PRELOAD="+os.fsencode(lib)}
    if any(s.startswith(b"LD_PRELOAD=") and s not in allowed for s in environ):
        raise RuntimeError("服务已有 LD_PRELOAD，保留现有配置并停止本次试用。")
def stage():
    payload=(HERE/"libfnbridge.so").read_bytes()
    if hashlib.sha256(payload).hexdigest()!=SO_HASH: raise RuntimeError("中转库校验失败")
    if not SO.exists():
        with tempfile.NamedTemporaryFile(dir=STAGE,delete=False) as f:
            tmp=Path(f.name)
            f.write(payload)
        try:
            tmp.chmod(0o644)
            os.link(tmp,SO)
        finally:
            tmp.unlink()
    if SO.is_symlink() or digest(SO) not in (SO_HASH,PREVIEW_SO_HASH): raise RuntimeError("暂存库不匹配")
def test_format(fmt,index):
    with_scale=index<2
    which="--case" if with_scale else "--transfer"
    final_label="staged_output" if with_scale else "reverse_readback"
    for mode in ("split","default"):
        env=dict(os.environ,LD_PRELOAD=str(SO),FN_BRIDGE_SELFTEST="pixels",
                 FN_BRIDGE_MASK=str(1<<index))
        r=subprocess.run([sys.executable,"-I",str(HERE/"pixels.py"),which,mode,fmt],
                         env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
        out=r.stdout.decode(errors="replace")
        marks=[re.search(r"FNBRIDGE3 pid=\d+ "+direction+" fmt="+fmt+r" \d+x\d+ rc=0",out)
               for direction in ("CUDA->host->Vulkan","Vulkan->host->CUDA")]
        if r.returncode or not all(marks) or f"{mode} 3 {final_label} OK" not in out:
            print(out,flush=True)
            print(f"SKIP {fmt} {mode} exit={r.returncode}",flush=True)
            return False
    print("PASS",fmt,flush=True)
    return True
def rollback():
    if installed_mask() is not None:
        CONF.unlink()
        restart(False)
    elif live(True,verify_mask=False):
        restart(False)
    elif not live(False):
        raise RuntimeError("没有本试用包的配置，无法确认原服务状态")
    print("ROLLBACK OK",flush=True)
def logs():
    invocation=ctl("show","--value","-p","InvocationID",UNIT)
    cmd=["journalctl","-b","-u",UNIT,"--no-pager","-o","cat"]
    if invocation: cmd+=["_SYSTEMD_INVOCATION_ID="+invocation]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,check=True)
    hits=[s for s in r.stdout.decode(errors="replace").splitlines() if "FNBRIDGE3 pid=" in s]
    print("\n".join(hits[-30:]) if hits else "暂未从当前服务日志读到 FNBRIDGE。")
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=["check","apply","rollback","status"])
    action=p.parse_args().action
    if os.geteuid()!=0: raise RuntimeError("请在 NAS root 终端运行")
    if action=="status":
        print("配置中启用的帧格式:",format_names(installed_mask() or 0))
        print("主进程中转库已映射:",live(True))
        logs()
        return
    STAGE.mkdir(exist_ok=True,mode=0o755)
    if STAGE.is_symlink() or STAGE.stat().st_uid or STAGE.stat().st_mode & 0o022:
        raise RuntimeError("暂存目录权限异常")
    with (STAGE/"lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if action=="rollback":
            rollback()
            if SO.exists() and digest(SO) in (SO_HASH,PREVIEW_SO_HASH): SO.unlink()
            return
        if CONF.exists(): raise RuntimeError("已有试用配置，请先查看 status")
        stock()
        stage()
        mask=0
        for index,fmt in enumerate(FORMATS):
            try:
                passed=test_format(fmt,index)
            except subprocess.TimeoutExpired:
                print("SKIP",fmt,"timeout",flush=True)
                passed=False
            if passed: mask |= 1<<index
        print("PASS LIST",format_names(mask),flush=True)
        print("SKIP LIST",format_names(~mask) or "(none)",flush=True)
        if mask&3!=3: raise RuntimeError("基础 NV12/P010 预检未全部通过，保留当前服务")
        if action=="check": return
        if mask==3 and V2_CONF.exists():
            print("没有新增通过的格式，现有 NV12/P010 补丁已覆盖。")
            return
        CONF.parent.mkdir(exist_ok=True,mode=0o755)
        with tempfile.NamedTemporaryFile(mode="w",dir=CONF.parent,delete=False) as f:
            tmp=Path(f.name)
            f.write(config(mask))
        try:
            os.link(tmp,CONF)
        finally:
            tmp.unlink()
        try:
            restart(True)
        except BaseException:
            print("启用失败，正在恢复。",flush=True)
            rollback()
            raise
        print("BRIDGE ACTIVE",format_names(mask))
if __name__=="__main__":
    try: main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as err: sys.exit(str(err))
