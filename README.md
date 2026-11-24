# 飞牛影视中心 V100 vGPU 转码花屏补丁

在飞牛影视中心 **0.8.45（x86-64）** 上，NVIDIA Tesla V100-SXM2-16GB vGPU 转码生成的分片出现花屏。本补丁让 CUDA 和 Vulkan 之间的视频帧经系统内存中转，继续使用飞牛的 GPU 缩放和 NVENC 编码。独立像素测试在该设备上验证了 20 种候选帧格式中的 18 种。

[下载完整补丁及 SHA-256 校验文件](https://github.com/FurFlowery/fnnas-fix-vgpu-media/releases/latest) · [故障定位与源码修复建议](docs/diagnosis.md) · [测试环境、格式清单及限制](docs/testing.md)

## 实测环境

| 项目 | 配置 |
|---|---|
| 宿主机系统 | VMware ESXi 8.0 U3j，构建号 `25429389` |
| 宿主机 CPU / GPU | Intel(R) Xeon(R) Platinum 8175M / NVIDIA Tesla V100-SXM2-16GB |
| 宿主机 vGPU 驱动 | `580.159.01-1OEM.800.1.0.20613240` |
| 来宾机系统 / 应用 / vGPU | fnOS `1.2.1102`（x86-64），飞牛影视中心 `0.8.45`，vGPU 配置 `V100-2Q` |
| 来宾机驱动 | NVIDIA GRID `580.159.03`，来自 [fnnas/appstore.driver.gpu.nvidia.grid](https://github.com/fnnas/appstore.driver.gpu.nvidia.grid) |
| 实测驱动包 | [nvidia.ko-580.159.03-3-6.18.18.c1168-trim-amd64.tgz.fpk](https://github.com/fnnas/appstore.driver.gpu.nvidia.grid/releases/download/2026.11.02-15-19-45/nvidia.ko-580.159.03-3-6.18.18.c1168-trim-amd64.tgz.fpk) |

## 为什么会花屏

飞牛将 CUDA 解码帧送入 Vulkan 的 `scale_vulkan`，再将缩放结果传回 CUDA 编码。用合成 NV12 图案分别测试两个方向，直接传输有时会返回成功，但接收端读出的亮度和色度已经错误。强制拆分 Vulkan 平面后仍能复现。改用系统内存分别下载、上传，同样的图案在缩放前后逐行比对一致。

## 如何使用

从 Releases 下载 `FnOS-0.8.45-V100-20formats.zip` 和 `SHA256SUMS.txt`，放入 NAS 的 `/vol1`。在 NAS 的 root 终端执行：

```sh
cd /vol1
sha256sum -c SHA256SUMS.txt
unzip FnOS-0.8.45-V100-20formats.zip
cd v100-formats
python3 bridge_test.py check
```

`check` 读取三个厂商 ELF 的散列，在独立进程中测试 20 种格式，输出 `PASS LIST` 和 `SKIP LIST`。预检会使用 GPU 资源。准备启用时，在同一目录执行：

```sh
python3 bridge_test.py apply
```

`apply` 会重新预检，只为两种 Vulkan 布局下均通过的帧格式启用中转。基础 NV12 或 P010LE 失败时保留当前服务。启用成功时会重启一次影视中心并中断播放，请在空闲时操作。已有第三版试用包且运行正常的设备可以继续使用现有安装。

新开一次需要缩放的转码，查看新分片和当前工作进程命中的 `fmt=`：

```sh
python3 bridge_test.py status
```

需要撤销时运行 `python3 bridge_test.py rollback`。补丁使用服务专用的 `LD_PRELOAD`，原始文件保留在 `/usr/trim`。服务配置位于 `/run/systemd/system/mediasrv.service.d/97-fn-v100-host-bridge-v3.conf`，NAS 重启后会清除。若之前有已识别的 NV12 或 NV12/P010 版配置，本版回滚时会恢复到对应版本。

## 覆盖范围

在飞牛这版 CUDA 后端列出的 20 种软件帧格式中，以下 18 种在目标 NAS 上通过了两个传输方向的逐像素比对。表中的 PASS 表示中转路径可用。原版直接传输的像素损坏已在 NV12 上独立复现，其余格式的原路径是否会损坏需逐项验证。

| 飞牛 0.8.45 / V100 实测 | 软件帧格式 |
|---|---|
| PASS · 半平面 YUV（6） | `nv12`、`nv16`、`p010le`、`p016le`、`p210le`、`p216le` |
| PASS · 平面 YUV（8） | `yuv420p`、`yuv422p`、`yuv444p`、`yuva420p`、`yuv420p10le`、`yuv422p10le`、`yuv444p10le`、`yuv444p16le` |
| PASS · 打包 RGB（4） | `bgr0`、`rgb0`、`bgra`、`rgba` |
| SKIP · 飞牛 Vulkan 帧池不支持（2） | `yuv444p10msble`、`yuv444p12msble` |

**两种 SKIP 的原因：** 这两个格式都出现在飞牛的 CUDA 格式表中，但在本次测试的拆分平面模式下，飞牛自带的 Vulkan 后端创建帧池时直接报告 `Unsupported pixel format` 和 `Invalid argument (-22)`。错误发生在中转调用之前，补丁会跳过这两项。名称相近的 `yuv444p10le` 已通过预检。

补丁按每次传输两端的硬件类型和软件帧格式匹配。不同编码的片源只要经过同一条 CUDA/Vulkan 路径，就能使用对应格式的中转。实片验证包括早期 NV12 版的 H.264 转码，以及 NV12/P010 版的 H.265 片源。20 格式版在同一台 NAS 上完成了预检，结果为 18 通过、2 跳过。AV1 实片、HDR 色准及并发吞吐尚待验证。额外的系统内存拷贝可能影响性能。

## 源码和构建

`bridge.c` 实现帧中转，`bridge.map` 定义导出符号。控制脚本在 `bridge_test.py`，逐像素测试在 `pixels.py`。正式包附有预编译的 Linux x86-64 共享库。开发者可以使用 Python 3 和 Zig 0.14.1 从源码构建：

```sh
python3 tools/build_release.py
```

输出位于 `dist/`。构建脚本把本次构建的库散列写入 ZIP 内的控制脚本，并生成包内和包外校验文件。控制脚本使用此构建对应的 ABI 和三个精确的厂商文件散列；适配其他版本时需要重新核对布局和调用点。

本项目的中转库、控制脚本和诊断脚本使用 [MIT 许可证](LICENSE)。

# 目前此问题已经提交给官方，不知道官方会不会修复

理论上如果飞牛继续使用**ffmpeg8.1.1**的定制版（飞牛在里面加了scale_vulkan），并且没有做修复，只要是**NVIDIA**的显卡在转码时都有可能触发这个问题

问题存在于 `CUDA↔Vulkan 直接传帧` 的过程中，

ffmpeg有过类似问题的提交记录，但在9.0中才合并进正式版中
