# 测试环境与结果

| 项目 | 实测环境 |
|---|---|
| 平台 | 飞牛 NAS，Linux x86-64，systemd 管理的 `mediasrv.service` |
| 应用 | 飞牛影视中心 0.8.45 |
| GPU | NVIDIA Tesla V100-SXM2-16GB，vGPU 环境；应用日志识别设备 ID 为 `10de:1db1` |
| 媒体库 | 飞牛定制 `8.1.1-mediasrv`，`libavutil.so.60`、`libavfilter.so.11` |
| 文件系统 | 该机 `/run` 带 `noexec`，补丁库暂存在 `/var/lib/fn-v100-host-bridge-v3` |
| 未记录信息 | fnOS 系统小版本、内核版本、宿主机和来宾机驱动版本、vGPU profile |

### 精确适配的厂商文件

| ELF | SHA-256 |
|---|---|
| `mediasrv` | `e3c381af4db916e4b29f1ee9c7a2c64afaa6aeff3cc14bbd0969b8597315ac35` |
| `libavutil.so.60` | `09f6706ca66afa479553cf0957eded05d194456dd9c8badb130610162fc1770c` |
| `libavfilter.so.11` | `21ab53dc2e1d6a0cb560998fac778fd20611b5a0159a0a55c67b1a0496e65015` |

### 20 格式版在目标 NAS 上的预检

| 结果 | 帧格式 |
|---|---|
| PASS，半平面 YUV（6） | `nv12`、`nv16`、`p010le`、`p016le`、`p210le`、`p216le` |
| PASS，平面 YUV（8） | `yuv420p`、`yuv422p`、`yuv444p`、`yuva420p`、`yuv420p10le`、`yuv422p10le`、`yuv444p10le`、`yuv444p16le` |
| PASS，打包 RGB（4） | `bgr0`、`rgb0`、`bgra`、`rgba` |
| SKIP（2） | `yuv444p10msble`、`yuv444p12msble` |

通过的每一项都使用三组图案，在默认 Vulkan 布局和拆分布局下分别校验 CUDA→Vulkan 与 Vulkan→CUDA。两个方向从独立上传的已知像素出发，另测了 1088 行帧池内 1080 行可见图像。NV12 和 P010LE 还验证了定制 `scale_vulkan` 的缩放回读。两项跳过的格式在拆分布局的 Vulkan 帧池初始化时返回 `Unsupported pixel format` 和 `EINVAL (-22)`，脚本据此跳过后续测试。

### 实际视频

H.264 用例在 NV12 版上从 1920×1080 转至 1280×720，两个方向都有中转命中记录，用户确认新分片画面正常。使用 NV12/P010 版复测此前花屏的 H.265 片源，用户也确认画面恢复正常。该次会话的逐方向格式日志未保存。20 格式版的 18 项 PASS 记录来自合成帧预检，尚无覆盖全部格式的实片与性能数据。
