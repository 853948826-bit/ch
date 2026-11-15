# 用图像模型一次生成表情拼图（参考图 → 皮肤）

适用：Meta Muse Image、GPT 图像、即梦/豆包、Gemini、Midjourney 等支持“参考图”的模型。上传你的形象素材图作为参考，粘贴下面的提示词，一次生成一张 3×2 拼图，再用套件切分：

```bash
python3 tools/make_skin.py --sheet 拼图.png --grid 3x2 --name 我的形象 --apply
```

## 提示词模板（中文）

> 以参考图中的角色为唯一主体，严格保持其造型、比例、配色、材质与画风完全一致，不新增任何服饰或配件。
> 生成一张 3 列 × 2 行的角色表情拼图，纯白背景，每格一个完整全身角色，大小一致、居中、底部对齐，格子之间留白，不要文字和边框。
> 顺序（从左到右、从上到下）：1 平静默认；2 开心（眯眼微笑）；3 担心（皱眉）；4 惊讶（睁大眼张嘴）；5 睡着（闭眼）；6 被拎起慌张。
> 只改变表情和轻微姿态，身体朝向与参考图相同。

## Prompt template (English)

> Use the character in the reference image as the only subject; keep its shape, proportions, colors, material and rendering style identical, add no accessories.
> Create a 3-column × 2-row expression sheet on a pure white background, one full-body character per cell, same size, centered and bottom-aligned, with spacing, no text, no borders.
> Order (left→right, top→bottom): 1 neutral; 2 happy (smiling eyes); 3 worried; 4 surprised (wide eyes, open mouth); 5 asleep (eyes closed); 6 startled while being picked up.
> Change only the expression and slight pose; keep the same facing direction as the reference.

## 成本与技巧

- 一次生成 = 6 个状态，常见模型单张约 ¥0.1–1；不满意只重生成拼图，不用改代码。
- 角色走形时：减少一次要求的状态数（如 2×2），或把满意的那格再作为参考图重新生成。
- 背景务必纯色（白最佳），套件会自动抠图；残留时调大 `--tol`。
