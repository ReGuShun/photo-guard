# 发布整理与验证记录

- 本次整理为独立目录，不修改原服务器运行代码。
- 发布版移除闲置 FaceFusion 修复函数，保留独立 YOLOFace 检测模型接口。
- 固定 MusePose 与 Dormant 上游提交号，并保留所需本地变更补丁。
- 两份补丁均在对应上游提交的临时干净副本中通过应用检查；修改后的 Python 源码通过语法检查。
- 前端依赖清单与服务器安装版本记录分开保存，后者不是完整复现保证。
- 分享版原型素材重新绘制，不包含用户上传人物照片、视频或调查问卷。
- 本次未完成跨机器依赖安装及 GPU 端到端推理；防护有效性与压缩鲁棒性不在本次检查范围内。

## 上传 GitHub

仓库默认设为私有。已安装 GitHub CLI 时：

```bash
gh auth login
bash scripts/publish_github.sh
```

脚本使用登录账号创建 `photo-protection-platform` 仓库。确认需要公开后可在 GitHub 设置中自行变更可见性。
