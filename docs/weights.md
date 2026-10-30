# 模型文件与来源

权重不随 Git 仓库分发。准备上游代码后，先阅读各上游 README 的下载和许可说明。

## Dormant/pretrained_weights

- `DWPose/{yolox_l.onnx,dw-ll_ucoco_384.onnx}`：DWPose 官方资源。
- `AnimateAnyone/{denoising_unet.pth,reference_unet.pth,pose_guider.pth,motion_module.pth}`：Dormant README 指定的 AnimateAnyone 权重。
- `MagicAnimate/appearance_encoder/`：包含 config.json 与模型权重。
- `MagicPose/model_state-110000.th`：Dormant README 指定来源。
- `stable-diffusion-v1-5/`：包含 UNet、文本编码器、分词器等配置和权重。
- `sd-vae-ft-mse/`：VAE 配置和权重。
- `sd-image-variations-diffusers/`：UNet 和 image_encoder 的配置和权重。

另需 `torch_cache/hub/checkpoints/vgg16-397923af.pth`（LPIPS 所用 VGG16）。

## MusePose/pretrained_weights

`MusePose/{denoising_unet.pth,reference_unet.pth,pose_guider.pth,motion_module.pth}`，按 MusePose 官方 README 下载。此集成复用 Dormant 目录中的基础模型。

## 对齐模型

- `models/yolo/yolov8n.pt`：Ultralytics 人物检测。
- `models/yolo/yoloface_8n.onnx`：YOLOFace 人脸检测；原系统使用 FaceFusion 发布的同名检测模型，不需要安装完整 FaceFusion。

可通过 `.env` 覆盖两个检测模型路径。界面会检查主要推理权重，但仍需实际启动验证版本兼容、显存和模型内容。
