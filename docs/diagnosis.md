# 故障定位与修复思路

在同一台 V100 vGPU 上，独立 FFmpeg 的 CUDA 解码 → `scale_cuda` → NVENC 路径工作正常。飞牛影视中心把解码帧传给 Vulkan 做缩放，再传回 CUDA。对原版库的单向回读表明，NV12 像素损坏在编码前已经发生。驱动、虚拟化层及定制 FFmpeg 之间的具体责任仍需结合源码和对应驱动版本确认。

## 定位依据

- 在这一构建的 `media::prepare_nvidia_vulkan_frame(StreamMap*, AVFrame*)` 中，ELF 虚拟地址 `0x10e912` 调用 `av_hwframe_transfer_data(dst_vulkan, src_cuda, 0)`。NVIDIA 输出滤镜链可在定制 `scale_vulkan` 后追加 `hwupload=derive_device=cuda`。
- 使用原版库和 NV12 测试图案，CUDA、Vulkan 各自上传及回读均正确。默认 Vulkan 图像布局出现过 `cuExternalMemoryGetMappedMipmappedArray` 返回 `CUDA_ERROR_INVALID_VALUE`。启用 `disable_multiplane=1` 后，两个方向均可完成调用，但三轮测试中，每轮单向回读均有 1088 行 Y 和 544 行 UV 与输入不同。直接往返却能还原原图，单做往返测试会漏检。
- 将两侧改为系统内存中转，保留定制缩放滤镜，三组 NV12 图案在 CUDA 下载、Vulkan 输入、缩放输出与最终 CUDA 回读四处均零差异。真实播放结果见[测试记录](testing.md)。

在匹配构建的 NAS 上，从仓库根目录运行以下独立进程测试可对照原库和系统内存路径。两者使用合成帧，首条命令在原问题环境出现 DIFF 属预期结果。

```sh
python3 tests/v100_frames.py --case split
python3 tests/v100_host_scale.py --case split
```

## 产品侧修复

建议将兼容路径集成到飞牛的帧传输层。输入执行 CUDA → 同格式系统内存帧 → Vulkan，输出执行 Vulkan → 同格式系统内存帧 → CUDA。按两端 `AVHWFramesContext.sw_format` 匹配，正确处理逐平面步长、位深、透明度、分配尺寸与可见尺寸。在受影响的组合上提前选择该路径，或先分别执行两个方向的固定图案回读，再决定是否走直接传输。API 返回成功本身不能保证像素正确。

若要恢复直接互操作的性能，可核对实际 VkImage 格式、平面与图像数量、外部显存导出能力、拷贝描述及同步。上游 FFmpeg 的半平面导入检查与实际图像格式查询修改可用于源码对照；飞牛样本已包含读取实际格式的逻辑，仍需确认其备用查询分支。[半平面 CUDA 导入检查](https://github.com/FFmpeg/FFmpeg/commit/c29d710cd5d0f80bbb56f7ec1f35d5fb7ed44d05) · [使用实际图像格式查询导出能力](https://github.com/FFmpeg/FFmpeg/commit/0baa71b53cd9dc6ecf0cf9a5c271bcbde04135ba)
