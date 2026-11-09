#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CraftLink（方块联机）图标资源生成器。

一条命令生成全部品牌图（不联网、不下载、不需要任何字体文件）：

    python scripts/make_assets.py

产物：
    client/mclink/assets/app.ico     多尺寸 Windows 图标（16/24/32/48/64/128/256）
    client/mclink/assets/app.png     256x256 应用图标（与 ico 同一母版）
    panel/web/assets/avatar.png      256x256 面板头像（原创方块小人头，透明背景）
    docs/shots/logo-preview.png      预览排版图（给人类确认用）

设计要点 / 硬约束：
  * 全部用 PIL.ImageDraw 从零画，几何自己算，**不使用任何 Mojang 素材**；
  * 1024 px 母版超采样渲染，再用 LANCZOS 缩到 16..256，小尺寸才不糊；
    16/24/32/48 额外做一次轻微 USM 锐化（只锐 RGB，不碰 alpha，避免白边）；
  * 图标内不放文字（界面上它旁边就是 CraftLink 字样）；
  * 圆角方块 + 竖向渐变底 + 深色描边 + 柔光高光，深浅两种底色下都能看；
  * 完全确定性：没有随机数、没有时间戳，同样的输入永远得到同样的字节。
    唯一可选参数是 --variant，只影响"草方块"的画法，默认值是实测后选定的。

踩过的坑（别再犯）：Image.alpha_composite 会返回**新图**，如果之前建的
ImageDraw 还指着旧图，后面所有绘制都会画进一张被丢掉的图层里（表现为
"画了但没出现"）。所以本文件统一用下面的 Canvas 小类管理当前图层，
每次合成/模糊之后都会重建 ImageDraw。
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# 路径与尺寸常量
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # mc-link-v1/

OUT_ICO = os.path.join(ROOT, "client", "mclink", "assets", "app.ico")
OUT_APP_PNG = os.path.join(ROOT, "client", "mclink", "assets", "app.png")
OUT_AVATAR = os.path.join(ROOT, "panel", "web", "assets", "avatar.png")
OUT_PREVIEW = os.path.join(ROOT, "docs", "shots", "logo-preview.png")

MASTER = 1024                                     # 母版边长 = 256 * 4
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)        # Windows 图标要的帧
APP_PNG_SIZE = 256
AVATAR_SIZE = 256
PREVIEW_AVATAR_SIZE = 128

# ---------------------------------------------------------------------------
# 调色板（呼应面板现有配色：主蓝 #4c8dff、草绿、土棕）
# ---------------------------------------------------------------------------
SKY_TOP = "#2F6FED"        # 渐变底：上
SKY_BOT = "#4C8DFF"        # 渐变底：下
SKY_DEEP = "#0F2E6E"       # 底部压暗用
GLOW = "#9CC6FF"           # 方块背后的柔光
RING = "#0B1F52"           # 圆角方块外圈深色描边

GRASS_TOP = "#77D24F"      # 顶面草绿（最亮）
GRASS_LEFT = "#5DBE3F"     # 左侧草皮
GRASS_RIGHT = "#47A02C"    # 右侧草皮（背光）
GRASS_DARK = "#2F7520"     # 草皮暗边 / 草的土交界阴影

DIRT_LEFT = "#96602F"      # 左侧泥土（受光）
DIRT_RIGHT = "#6B421C"     # 右侧泥土（背光）
DIRT_DARK = "#4A2C0E"      # 泥土暗部

# 头像用色（原创，不使用 Mojang 皮肤纹理）
SKIN = "#E3A87C"
SKIN_LIGHT = "#F3C79D"
SKIN_SHADOW = "#C98A5F"
SKIN_DEEP = "#A96B45"
HAIR = "#3E2A1C"
HAIR_LIGHT = "#5C4029"
HAIR_DARK = "#281A11"
EYE_WHITE = "#F4F8FF"
PUPIL = "#22304A"
MOUTH = "#8C4A3C"
AVATAR_RING = "#241A12"

# 小尺寸"像素提示"（pixel hint）：16/24 px 下，草皮下沿的锯齿（7 段台阶）缩下去只剩
# 1~2 px，实测会糊成一片噪点；改成一条直线、草皮再厚一点，边界明显更干净。
# 32 px 往上锯齿反而更好看（有草皮垂下来的感觉），所以只有最小的两档走简化版。
SMALL_SIZES = (16, 24)
SMALL_FRINGE_BASE = 112        # 简化版草皮厚度（正常版是 96）
SMALL_FRINGE_DARK = 150        # 简化版草土交界暗缝的不透明度（正常版 130）

# 小尺寸锐化参数：size -> (radius, percent)
_SHARPEN = {
    16: (0.55, 120),
    24: (0.55, 95),
    32: (0.60, 70),
    48: (0.60, 45),
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def hex2rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def hexa(value: str, alpha: int) -> tuple:
    """把 #RRGGBB 变成带 alpha 的 RGBA 元组。"""
    return hex2rgb(value) + (max(0, min(255, int(alpha))),)


def mix(c1: tuple, c2: tuple, t: float) -> tuple:
    """两个 RGB 之间线性插值，t=0 取 c1。"""
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def blank(size=MASTER) -> Image.Image:
    """一张全透明的 RGBA 画布。"""
    return Image.new("RGBA", (size, size), (0, 0, 0, 0))


def over(base: Image.Image, layer: Image.Image) -> Image.Image:
    """标准 alpha 合成（layer 压在 base 上）。"""
    return Image.alpha_composite(base, layer)


def soft(layer: Image.Image, radius: float) -> Image.Image:
    """高斯模糊，做柔光/投影用。"""
    return layer.filter(ImageFilter.GaussianBlur(radius))


class Canvas:
    """"当前图层"管理器：flush() 合成，blur()/clip() 就地加工，d 永远有效。

    两条必须遵守的规矩（都踩过）：
    1. Image.alpha_composite 返回**新图**。若 ImageDraw 还指着旧图，后面的绘制
       全都会画进一张被丢掉的图层（表现为"画了但没出现"）。本类每次合成/模糊
       都会重建 ImageDraw，所以拿 c.d 画就永远不会画错地方。
    2. Pillow 的 ImageDraw 对 RGBA 图是**整像素替换**（含 alpha），只有 RGB 图
       才支持 "RGBA" 混合模式。所以往图层上画半透明色（alpha < 255）时，那一层
       必须还是空的；否则就是把已有像素抠成半透明 —— 底色会透出来（草土交界
       发蓝、头像右脸糊成泥，都是这么来的）。半透明形状一律单独 flush 一层。
    """

    def __init__(self, size: int = MASTER):
        self.size = size
        self.img = blank(size)
        self._reset()

    def _reset(self):
        self.layer = blank(self.size)
        self.d = ImageDraw.Draw(self.layer, "RGBA")

    def flush(self):
        self.img = over(self.img, self.layer)
        self._reset()
        return self

    def blur(self, radius: float):
        self.layer = soft(self.layer, radius)
        self.d = ImageDraw.Draw(self.layer, "RGBA")
        return self

    def clip(self, mask: Image.Image):
        """把当前图层的 alpha 乘上蒙版（裁形状用）。"""
        self.layer.putalpha(ImageChops.multiply(self.layer.getchannel("A"), mask))
        return self

    def alpha_from(self, mask: Image.Image):
        """当前图层整张换成"纯色 + 指定 alpha 蒙版"。"""
        self.layer.putalpha(mask)
        return self

    def recolor(self, color: str):
        """颜色整体换成 color，alpha 蒙版保持不变（配合 alpha_from 用）。"""
        alpha = self.layer.getchannel("A")
        self.layer = Image.new("RGBA", (self.size, self.size), hexa(color, 0))
        self.layer.putalpha(alpha)
        self.d = ImageDraw.Draw(self.layer, "RGBA")
        return self


def vertical_gradient(size: int, top: str, bottom: str) -> Image.Image:
    """竖向线性渐变（先画 1 像素宽的列再横向拉伸，快且确定）。"""
    col = Image.new("RGB", (1, size))
    px = col.load()
    c1, c2 = hex2rgb(top), hex2rgb(bottom)
    for y in range(size):
        px[0, y] = mix(c1, c2, y / (size - 1) if size > 1 else 0.0)
    return col.resize((size, size), Image.NEAREST)


def vertical_alpha(size: int, stops) -> Image.Image:
    """按 [(y, alpha), ...] 折线生成竖向 alpha 蒙版（柔光淡出用）。"""
    stops = sorted(stops)
    col = Image.new("L", (1, size))
    px = col.load()
    for y in range(size):
        if y <= stops[0][0]:
            v = stops[0][1]
        elif y >= stops[-1][0]:
            v = stops[-1][1]
        else:
            v = stops[-1][1]
            for i in range(len(stops) - 1):
                (ya, aa), (yb, ab) = stops[i], stops[i + 1]
                if ya <= y <= yb:
                    t = 0.0 if yb == ya else (y - ya) / (yb - ya)
                    v = aa + (ab - aa) * t
                    break
        px[0, y] = int(round(max(0, min(255, v))))
    return col.resize((size, size), Image.NEAREST)


def rounded_mask(size: int, box, radius: int) -> Image.Image:
    """圆角矩形蒙版（母版尺寸下画，边缘靠最终缩放得到抗锯齿）。"""
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def hband_mask(size: int, box, stops) -> Image.Image:
    """只在 box 的纵向范围内、按竖向折线做 alpha 的蒙版（局部压暗/反光用）。"""
    band = vertical_alpha(size, stops)
    m = Image.new("L", (size, size), 0)
    m.paste(band.crop((0, box[1], size, box[3])), (0, box[1]))
    return m


def downscale(master: Image.Image, size: int) -> Image.Image:
    """母版 -> 目标尺寸。小尺寸补一次轻微 USM，避免 16/32 px 糊成一团。"""
    img = master.resize((size, size), Image.LANCZOS)
    cfg = _SHARPEN.get(size)
    if cfg:
        radius, percent = cfg
        rgb = img.convert("RGB").filter(
            ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=2)
        )
        img = Image.merge("RGBA", rgb.split() + (img.getchannel("A"),))
    return img


# ---------------------------------------------------------------------------
# 图标一：等轴测草方块（默认方案）
# ---------------------------------------------------------------------------
def _iso_fringe(p_start, p_end, base: float, depths) -> list:
    """沿一条斜边生成"草皮下沿带锯齿"的多边形。

    p_start / p_end 是侧面与顶面共用的那条上沿；base 是草皮基础厚度，
    depths 是每段锯齿额外下垂的高度（硬编码常量 -> 可复现）。
    点序：先沿上沿走过去，再沿带台阶的下沿走回来。
    """
    n = len(depths)
    x0, y0 = p_start
    x1, y1 = p_end
    pts = [(x0, y0), (x1, y1)]
    for i in range(n - 1, -1, -1):                 # 从右往左回走
        xa = x0 + (x1 - x0) * (i + 1) / n
        ya = y0 + (y1 - y0) * (i + 1) / n
        xb = x0 + (x1 - x0) * i / n
        yb = y0 + (y1 - y0) * i / n
        dep = base + depths[i]
        pts.append((xa, ya + dep))                 # 竖直下落
        pts.append((xb, yb + dep))                 # 水平走一格（锯齿台阶）
    return pts


def _iso_layer(small: bool = False) -> Image.Image:
    """等轴测（2:1）草方块：顶面草绿、两侧泥土 + 顶部草皮 + 地面接触阴影。

    small=True 时走"像素提示"版：草皮下沿取直、草皮略厚，专供 16/24 px。
    """
    M = MASTER
    cx, s, k, h = 512, 352, 176, 352              # 半宽 / 菱形半高 / 侧面高度
    top = 176
    cy0 = top + k                                  # 顶面菱形中心
    N = (cx, top)
    E = (cx + s, cy0)
    S = (cx, cy0 + k)
    W = (cx - s, cy0)
    Sb = (S[0], S[1] + h)
    Wb = (W[0], W[1] + h)
    Eb = (E[0], E[1] + h)

    c = Canvas(M)

    # --- 地面接触阴影：让方块"坐"在底板上而不是飘着。刻意压得很淡 ——
    #     早先版本 alpha 135 时，16/32 px 下整块底板下半部会被糊成一团脏蓝。
    c.d.ellipse((228, 838, 796, 916), fill=hexa("#07205C", 72))
    c.blur(34).flush()

    # --- 两个侧面，分三遍画（每遍一层）：
    #     1) 不透明泥土；2) 草土交界的半透明暗缝；3) 不透明草皮（带锯齿下沿）。
    #     半透明色**必须单独占一层**，原因见 Canvas 的注释。
    depths = [0] * 7 if small else [46, 6, 30, 12, 52, 0, 34]
    fringe_base = SMALL_FRINGE_BASE if small else 96
    fringe_dark = SMALL_FRINGE_DARK if small else 130
    faces = (
        ([W, S, Sb, Wb], W, S, DIRT_LEFT, GRASS_LEFT),
        ([S, E, Eb, Sb], S, E, DIRT_RIGHT, GRASS_RIGHT),
    )
    for quad, _, _, dirt, _ in faces:
        c.d.polygon(quad, fill=hexa(dirt, 255))
    c.flush()
    for _, p0, p1, _, _ in faces:
        c.d.polygon([(x, y + 24) for x, y in _iso_fringe(p0, p1, fringe_base, depths)],
                    fill=hexa(GRASS_DARK, fringe_dark))
    c.flush()
    for _, p0, p1, _, grass in faces:
        c.d.polygon(_iso_fringe(p0, p1, fringe_base, depths), fill=hexa(grass, 255))
    c.flush()

    # 注：这里曾经画过"泥土颗粒"（半透明白/黑小方块）。实测在 256 px 下像
    # 破洞，在 16 px 下纯属噪点，与"flat + soft shading"的方向相反，已删除。

    # --- 顶面：亮草绿打底，左上受光提亮，右下压暗
    c.d.polygon([N, E, S, W], fill=hexa(GRASS_TOP, 255))
    c.flush()
    c.d.polygon([N, W, (cx, cy0)], fill=hexa("#FFFFFF", 40))
    c.d.polygon([E, S, (cx, cy0)], fill=hexa("#1E5C12", 34))
    c.flush()

    # --- 轮廓线：整块外形描深，顶面/侧面交界再压一道，保证小尺寸也能分面
    c.d.line([N, W, S, E, N], fill=hexa(GRASS_DARK, 120), width=5, joint="curve")
    c.d.line([W, S, E], fill=hexa("#255F16", 100), width=6)
    c.flush()

    return c.img


# ---------------------------------------------------------------------------
# 图标二：圆角方形草皮面（备选，16 px 下色块面积最大）
# ---------------------------------------------------------------------------
def _tile_layer(small: bool = False) -> Image.Image:
    """圆角方形"草方块正面"：上面约 4 成草皮（锯齿下沿），下面泥土，无透视。"""
    M = MASTER
    box = (182, 186, 842, 846)                     # 与等轴测方案同宽同高
    radius = 96
    x0, y0, x1, y1 = box
    side = x1 - x0
    mask = rounded_mask(M, box, radius)

    c = Canvas(M)
    c.d.ellipse((206, 792, 818, 904), fill=hexa("#07205C", 135))
    c.blur(26).flush()

    # 泥土底：竖向渐变（不透明）
    c.layer.paste(vertical_gradient(side, "#9A6431", "#714620").convert("RGBA"), (x0, y0))
    c.flush()

    # 右侧背光（半透明 -> 单独一层；每条竖带互不重叠）
    for x in range(x0, x1, 4):
        t = max(0.0, (x - (x0 + side * 0.45)) / (side * 0.55))
        c.d.rectangle((x, y0, x + 4, y1), fill=hexa("#000000", int(58 * t)))
    c.clip(mask).flush()

    # 草皮：占上面 4 成（小尺寸档取直、加厚），下沿带锯齿
    cap = y0 + int(side * (0.46 if small else 0.40))
    depths = [0] * 9 if small else [0, 30, 10, 44, 16, 52, 8, 34, 18]
    n = len(depths)
    step = side / n
    pts = [(x0, y0), (x1, y0), (x1, cap)]
    for i in range(n - 1, -1, -1):
        pts.append((x0 + step * (i + 1), cap + depths[i]))
        pts.append((x0 + step * i, cap + depths[i]))
    c.d.polygon([(x, y + 22) for x, y in pts],
                fill=hexa(GRASS_DARK, 150 if small else 140))
    c.d.polygon(pts, fill=hexa(GRASS_LEFT, 255))
    c.clip(mask).flush()

    # 草皮左上提亮 / 右侧压暗（与等轴测方案同一套光照方向）
    c.d.polygon([(x0, y0), (x0 + side * 0.55, y0), (x0 + side * 0.30, cap + 20),
                 (x0, cap + 10)], fill=hexa("#FFFFFF", 44))
    c.d.polygon([(x1, y0), (x1, cap + 30), (x0 + side * 0.62, cap + 14),
                 (x0 + side * 0.72, y0)], fill=hexa(GRASS_DARK, 80))
    c.clip(mask).flush()

    c.clip(mask).flush()

    # 底部压暗 + 内描边 + 顶沿高光
    c.alpha_from(hband_mask(M, box, [(int(y0 + side * 0.70), 0), (y1, 52)]))
    c.recolor("#000000").clip(mask).flush()

    c.d.rounded_rectangle(box, radius=radius, outline=hexa(DIRT_DARK, 160), width=9)
    c.d.rounded_rectangle((x0 + 12, y0 + 12, x1 - 12, y1 - 12), radius=radius - 10,
                          outline=hexa("#FFFFFF", 56), width=9)
    c.clip(mask).flush()

    return c.img


LOGO_VARIANTS = {"iso": _iso_layer, "tile": _tile_layer}
# 实测结论（两种都渲出来，在 16/24/32/64/128/256 逐档比过）：
#   * 等轴测 iso：24 px 往上都能一眼看出"立体的草方块"，最像现代 App 图标；
#   * 平铺 tile：色块面积更大，但 16/24 px 下会塌成"绿帽子扣在棕块上"，偏平；
#   * 所以默认 iso，tile 只作为 --variant 备选留着（同一套底板，随时可换）。
DEFAULT_VARIANT = "iso"


def render_logo_master(variant: str = DEFAULT_VARIANT, small: bool = False) -> Image.Image:
    """渲染 1024 px 图标母版：圆角渐变底板 + 柔光 + 草方块。

    small=True 只把草方块换成"像素提示"版（见 SMALL_SIZES 的注释），
    底板、渐变、高光完全一致，所以大小尺寸看起来仍是同一个图标。
    """
    M = MASTER
    box = (22, 22, M - 22, M - 22)
    radius = 220
    tile_mask = rounded_mask(M, box, radius)

    # 1) 外圈深色描边：先画一张比底板大 4 px 的深色圆角块，底板压在上面
    c = Canvas(M)
    c.d.rounded_rectangle((box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4),
                          radius=radius + 4, fill=hexa(RING, 150))
    c.flush()

    # 2) 渐变底板
    c.layer = vertical_gradient(M, SKY_TOP, SKY_BOT).convert("RGBA")
    c.d = ImageDraw.Draw(c.layer, "RGBA")
    c.clip(tile_mask).flush()

    # 3) 草方块背后的柔光
    c.d.ellipse((140, 160, 884, 810), fill=hexa(GLOW, 84))
    c.blur(95).clip(tile_mask).flush()

    # 4) 顶部柔光 + 左上小光斑 + 底部压暗（都压得很轻，免得 16 px 发灰）
    c.alpha_from(vertical_alpha(M, [(22, 46), (300, 15), (540, 0)]))
    c.recolor("#FFFFFF").clip(tile_mask).flush()

    c.d.ellipse((100, 100, 450, 350), fill=hexa("#FFFFFF", 36))
    c.blur(62).clip(tile_mask).flush()

    c.alpha_from(vertical_alpha(M, [(540, 0), (M - 22, 104)]))
    c.recolor(SKY_DEEP).clip(tile_mask).flush()

    # 5) 内圈高光边（玻璃质感）
    c.d.rounded_rectangle((box[0] + 16, box[1] + 16, box[2] - 16, box[3] - 16),
                          radius=radius - 14, outline=hexa("#FFFFFF", 60), width=10)
    c.clip(tile_mask).flush()

    # 6) 草方块本体
    return over(c.img, LOGO_VARIANTS[variant](small=small))


# ---------------------------------------------------------------------------
# 头像：原创的方块小人头（8x8 网格，比例和 MC 头一致，但每个像素都是自己画的）
# ---------------------------------------------------------------------------
AV_BOX = 48                                        # 头部在母版里的内缩
AV_CELL = (MASTER - 2 * AV_BOX) // 8               # = 116，缩到 256 时正好 29 px


def _cell(col: int, row: int, fx0: float = 0.0, fy0: float = 0.0,
          fx1: float = 1.0, fy1: float = 1.0) -> tuple:
    """取第 (col,row) 格内的一个子矩形（分数坐标，方便画"半格"）。"""
    x = AV_BOX + col * AV_CELL
    y = AV_BOX + row * AV_CELL
    return (x + AV_CELL * fx0, y + AV_CELL * fy0,
            x + AV_CELL * fx1, y + AV_CELL * fy1)


# 8x8 面孔布局：
#   row0/1  头发整排            row4  皮肤 眼白 瞳孔 皮肤 皮肤 瞳孔 眼白 皮肤
#   row2    V 字刘海（中间额头） row5  皮肤 皮肤 皮肤 鼻影 鼻影 皮肤 皮肤 皮肤
#   row3    额头                row6  皮肤 皮肤 嘴 嘴 嘴 嘴 皮肤 皮肤
#                               row7  下巴（底部压暗 + 草绿反光）
FRINGE_COLS = (0, 1, 2, 5, 6, 7)


def render_avatar_master() -> Image.Image:
    """原创方块小人头：皮肤方块 + 像素眼 + 嘴 + 刘海，透明背景 + 深色描边。"""
    M = MASTER
    head = (AV_BOX, AV_BOX, M - AV_BOX, M - AV_BOX)
    head_mask = rounded_mask(M, head, 28)
    c = Canvas(M)

    # 1) 皮肤底 + 右下压暗 + 额头受光。三层分开画：半透明色块压在"同一层"的
    #    不透明底色上会被整像素替换成半透明（= 背景透出来，右脸糊成泥），
    #    必须各自成层再合成。alpha 也调过一轮：原来 90 太脏，现在 52/70。
    c.d.rectangle(head, fill=hexa(SKIN, 255))
    c.flush()
    for col in range(8):
        for row in range(8):
            if col >= 6 or row >= 6:
                c.d.rectangle(_cell(col, row), fill=hexa(SKIN_SHADOW, 52))
    c.flush()
    c.d.rectangle((AV_BOX, AV_BOX, M - AV_BOX, AV_BOX + AV_CELL), fill=hexa(SKIN_LIGHT, 70))
    c.flush()

    # 2) 头发：顶上两排整排 + 第二排 V 字刘海，左上高光、右侧背光
    c.d.rectangle((AV_BOX, AV_BOX, M - AV_BOX, AV_BOX + AV_CELL * 2), fill=hexa(HAIR, 255))
    for col in FRINGE_COLS:
        c.d.rectangle(_cell(col, 2), fill=hexa(HAIR, 255))
    c.d.rectangle((AV_BOX, AV_BOX, AV_BOX + AV_CELL * 4, AV_BOX + AV_CELL // 2),
                  fill=hexa(HAIR_LIGHT, 255))
    c.d.rectangle((AV_BOX, AV_BOX + AV_CELL, AV_BOX + AV_CELL * 2, AV_BOX + AV_CELL * 1.5),
                  fill=hexa(HAIR_LIGHT, 255))
    c.d.rectangle(_cell(6, 0, 0, 0, 2, 2), fill=hexa(HAIR_DARK, 255))
    c.flush()

    # 3) 刘海在额头上留一道很浅的阴影
    for col in FRINGE_COLS:
        c.d.rectangle(_cell(col, 3, 0, 0, 1, 0.22), fill=hexa("#3A2410", 76))
    c.flush()

    # 4) 眼睛：外眼白 + 内瞳孔（瞳孔朝鼻梁，和原版皮肤一个套路，但是自己画的）
    for col_white, col_pupil in ((1, 2), (6, 5)):
        c.d.rectangle(_cell(col_white, 4, 0, 0.14, 1, 0.9), fill=hexa(EYE_WHITE, 255))
        c.d.rectangle(_cell(col_pupil, 4, 0, 0.14, 1, 0.9), fill=hexa(PUPIL, 255))

    # 5) 鼻子（一小块鼻影）+ 嘴 + 下唇阴影。嘴原来是 0.84 格高的实心条，
    #    128 px 下像一撇胡子，改成 0.52 格细线并压暗颜色，干净很多。
    c.d.rectangle(_cell(3, 5, 0, 0.44, 2, 1.0), fill=hexa(SKIN_SHADOW, 150))
    c.d.rectangle(_cell(2, 6, 0, 0.26, 4, 0.78), fill=hexa(MOUTH, 255))
    c.d.rectangle(_cell(2, 6, 0, 0.78, 4, 0.96), fill=hexa(SKIN_DEEP, 70))
    c.flush()

    # 6) 体积：右列 / 底排压暗，再叠一层右侧横向渐变（各占一层，避免互相替换）
    c.d.rectangle(_cell(7, 0, 0, 0, 1, 8), fill=hexa(SKIN_DEEP, 30))
    c.d.rectangle(_cell(0, 7, 0, 0, 8, 1), fill=hexa(SKIN_DEEP, 34))
    c.flush()
    for x in range(AV_BOX, M - AV_BOX, 4):
        t = max(0.0, (x - (AV_BOX + AV_CELL * 5.6)) / (AV_CELL * 2.4))
        c.d.rectangle((x, AV_BOX, x + 4, M - AV_BOX), fill=hexa(SKIN_DEEP, int(26 * t)))
    c.flush()

    # 7) 草绿反光（呼应图标草皮，只是贴底边的一线光）
    c.alpha_from(hband_mask(M, head, [(M - AV_BOX - int(AV_CELL * 0.34), 0),
                                      (M - AV_BOX, 74)]))
    c.recolor(GRASS_LEFT).clip(head_mask).flush()

    # 8) 裁成圆角方块 + 深色描边（浅色/深色背景上都有边界）
    c.clip(head_mask).flush()
    out = blank(M)
    ImageDraw.Draw(out).rounded_rectangle(head, radius=28, fill=hexa(AVATAR_RING, 255))
    out = over(out, c.img)
    ring = blank(M)
    ImageDraw.Draw(ring).rounded_rectangle(head, radius=28,
                                           outline=hexa(AVATAR_RING, 240), width=10)
    return over(out, ring)


# ---------------------------------------------------------------------------
# 预览排版图
# ---------------------------------------------------------------------------
def _load_font():
    """PIL 自带字体，不依赖任何 ttf 文件；新版 Pillow 可以指定字号。"""
    try:
        return ImageFont.load_default(size=14)
    except Exception:
        return ImageFont.load_default()


def build_preview(sizes: dict, avatar: Image.Image) -> Image.Image:
    """把各尺寸图标排成一行，深浅两种底各来一遍，再附上放大的 16/32/48 px。"""
    font = _load_font()
    page_w = 1120
    pad = 40                                        # 页面留白
    strip_pad = 26                                  # 长条内边距
    gap = 20
    label_h = 22
    strip_h = 256 + label_h + 34

    items = [(sizes[s], "%d px" % s) for s in (256, 128, 64, 48, 32, 16)]
    items.append((avatar, "avatar %d" % PREVIEW_AVATAR_SIZE))

    cols = [max(im.width, 78) for im, _ in items]
    row_w = sum(cols) + gap * (len(items) - 1) + 2 * gap + 1   # 中间留分隔线
    inner_w = page_w - 2 * pad - 2 * strip_pad
    assert row_w <= inner_w, "预览图一行放不下，调 page_w"

    zoom_items = [(sizes[16].resize((128, 128), Image.NEAREST), "16 px x8"),
                  (sizes[32].resize((128, 128), Image.NEAREST), "32 px x4"),
                  (sizes[48].resize((144, 144), Image.NEAREST), "48 px x3")]
    zoom_h = 144 + label_h + 30

    page_h = 74 + strip_h * 2 + 24 + zoom_h + pad
    page = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    pd = ImageDraw.Draw(page)

    pd.text((pad, 26), "CraftLink  /  app icon + panel avatar  -  generated by "
                       "scripts/make_assets.py", font=font, fill=(24, 30, 40))
    pd.text((pad, 46), "top strip: light background   |   middle strip: dark background"
                       "   |   bottom: nearest-neighbour zoom", font=font, fill=(110, 118, 132))

    def draw_strip(y: int, bg, border, fg, note: str):
        pd.rounded_rectangle((pad, y, page_w - pad, y + strip_h), radius=14,
                             fill=bg, outline=border, width=1)
        x = pad + strip_pad + (inner_w - row_w) // 2
        baseline = y + strip_h - strip_pad - label_h
        for i, ((im, label), cw) in enumerate(zip(items, cols)):
            if i == len(items) - 1:                  # 头像前面画一条竖分隔线
                sx = x - gap
                pd.line([(sx, y + strip_pad + 10), (sx, baseline + 6)], fill=border, width=1)
            cx = x + (cw - im.width) // 2
            page.paste(im, (cx, baseline - im.height), im)
            tw = pd.textlength(label, font=font)
            pd.text((x + (cw - tw) / 2, baseline + 6), label, font=font, fill=fg)
            x += cw + gap
        pd.text((pad + strip_pad, y + 10), note, font=font, fill=fg)

    y = 74
    draw_strip(y, (242, 244, 248), (208, 214, 226), (44, 52, 66), "LIGHT  #f2f4f8")
    y += strip_h + 24
    draw_strip(y, (17, 21, 28), (58, 68, 88), (201, 211, 228), "DARK   #11151c")
    y += strip_h + 24

    pd.rounded_rectangle((pad, y, page_w - pad, y + zoom_h), radius=14,
                         fill=(248, 249, 252), outline=(214, 219, 230), width=1)
    pd.text((pad + strip_pad, y + 10),
            "NEAREST ZOOM  -  how crisp the small sizes really are",
            font=font, fill=(44, 52, 66))
    zx = pad + strip_pad + 20
    zbase = y + zoom_h - strip_pad - label_h
    for im, label in zoom_items:
        page.paste(im, (zx, zbase - im.height), im)
        pd.text((zx, zbase + 6), label, font=font, fill=(44, 52, 66))
        zx += im.width + 60
    return page


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="生成 CraftLink 的图标/头像资源")
    ap.add_argument("--variant", choices=sorted(LOGO_VARIANTS), default=DEFAULT_VARIANT,
                    help="草方块画法（默认 %s）" % DEFAULT_VARIANT)
    args = ap.parse_args(argv)

    logo_master = render_logo_master(args.variant)
    logo_master_small = render_logo_master(args.variant, small=True)
    avatar_master = render_avatar_master()
    sizes = {s: downscale(logo_master_small if s in SMALL_SIZES else logo_master, s)
             for s in ICO_SIZES}
    avatar = downscale(avatar_master, AVATAR_SIZE)
    avatar_small = downscale(avatar_master, PREVIEW_AVATAR_SIZE)
    preview = build_preview(sizes, avatar_small)

    outputs = (OUT_ICO, OUT_APP_PNG, OUT_AVATAR, OUT_PREVIEW)
    for path in outputs:                            # 缺目录就建
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
            print("mkdir   %s" % _rel(parent))

    # app.png / avatar.png
    sizes[APP_PNG_SIZE].save(OUT_APP_PNG, format="PNG", optimize=True)
    avatar.save(OUT_AVATAR, format="PNG", optimize=True)

    # app.ico：每一帧都用我们自己在目标尺寸上缩放+锐化过的图，不让 Pillow 现场缩
    frames = [sizes[s] for s in ICO_SIZES if s != APP_PNG_SIZE]
    sizes[APP_PNG_SIZE].save(OUT_ICO, format="ICO",
                             sizes=[(s, s) for s in ICO_SIZES],
                             append_images=frames)

    preview.save(OUT_PREVIEW, format="PNG", optimize=True)

    # 自检：ico 里必须真的含 16/24/32/48/64/128/256 七帧
    with Image.open(OUT_ICO) as im:
        have = sorted(w for w, _ in im.info.get("sizes", set()))
    print("")
    print("wrote   %s   (%s)" % (_rel(OUT_ICO), ", ".join("%dpx" % s for s in have)))
    for path in (OUT_APP_PNG, OUT_AVATAR, OUT_PREVIEW):
        with Image.open(path) as im:
            print("wrote   %s   (%dx%d %s)" % (_rel(path), im.width, im.height, im.mode))
    print("")
    print("variant=%s  master=%dpx  supersample=%dx  small-hint=%s  total=%d bytes"
          % (args.variant, MASTER, MASTER // APP_PNG_SIZE,
             ",".join("%d" % s for s in SMALL_SIZES),
             sum(os.path.getsize(p) for p in outputs)))
    print("预览图 docs/shots/logo-preview.png 里 16/32/48 px 那三格是 NEAREST 放大，"
          "放大倍数写在标签上；深浅两条分别对应 #f2f4f8 / #11151c 两种界面底色。")

    missing = [s for s in ICO_SIZES if s not in have]
    if missing:
        print("ERROR: app.ico 缺少帧 %s" % missing, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
