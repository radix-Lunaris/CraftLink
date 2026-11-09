"""
CraftLink-Builder -- 零依赖图片工具：识别图片 + 由 PNG 生成 `.ico`。

为什么需要它：

  * 安装器（`SetupIconFile`）和 Windows 快捷方式只认 `.ico`；
  * 开发者手里通常只有一个 logo `.png`；
  * 而 Builder 必须**离线**、**不装第三方库**（`client` 的打包脚本明确把
    PIL 排除在外，Builder 也不该为了转个图标就引入依赖）。

实现的是最小可用集合：

  * PNG：bit depth 1/2/4/8/16，color type 0/2/3/4/6，非隔行（Adam7 不支持，
    会明确报错让人换一张图，不静默产出坏图）；
  * ICO：32 位 BGRA + AND 掩码的**位图**条目（不用 PNG 压缩条目 —— 老版本
    Inno Setup / Windows 对 PNG 条目的支持不一致，宁可文件大一点也不踩这个坑）。

于是 `.ico` 在任何 Windows 版本上都能被正确加载，`file(1)` 也认。
"""

import os
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICO_SIGNATURE = b"\x00\x00\x01\x00"

#: 默认生成的图标尺寸（16 任务栏小图标 / 32 任务栏 / 48 资源管理器 / 256 大图标）。
DEFAULT_ICO_SIZES = (16, 32, 48, 256)

_CHANNELS = {
    0: 1,   # grayscale
    2: 3,   # truecolor
    3: 1,   # palette index
    4: 2,   # grayscale + alpha
    6: 4,   # truecolor + alpha
}


class ImageError(Exception):
    """图片读不了/不支持（消息直接给用户看）。"""


# ============================================================================
#  识别
# ============================================================================

def sniff(path):
    """
    看一眼文件头，判断是什么、多大。

    返回 `{"kind": "png"|"ico"|"unknown", "width": int, "height": int,
    "detail": str}`；读不了就 `kind="unknown"`（不抛异常，给校验器用）。
    """
    info = {"kind": "unknown", "width": 0, "height": 0, "detail": ""}

    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        info["detail"] = f"读不到文件：{exc}"

        return info

    if head.startswith(PNG_SIGNATURE):
        info["kind"] = "png"

        try:
            width, height, depth, color, _comp, _filter, interlace = struct.unpack(
                ">IIBBBBB", head[16:29]
            )
            info["width"] = width
            info["height"] = height
            info["detail"] = (
                f"{width}x{height} depth={depth} color={color} "
                f"interlace={interlace}"
            )
        except struct.error:
            info["detail"] = "PNG 头不完整"

        return info

    if head.startswith(ICO_SIGNATURE):
        info["kind"] = "ico"

        try:
            entries = ico_entries(path)
            info["width"] = max((entry["width"] for entry in entries), default=0)
            info["height"] = max((entry["height"] for entry in entries), default=0)
            info["detail"] = f"{len(entries)} 个尺寸：" + ",".join(
                f"{entry['width']}x{entry['height']}" for entry in entries
            )
        except ImageError as exc:
            info["detail"] = str(exc)

        return info

    info["detail"] = "不是 PNG 也不是 ICO（文件头对不上）"

    return info


def ico_entries(path):
    """解析 .ico 目录。返回条目列表；文件坏了抛 `ImageError`。"""
    with open(path, "rb") as fh:
        data = fh.read()

    if len(data) < 6 or data[:4] != ICO_SIGNATURE:
        raise ImageError("不是合法的 .ico（文件头不对）")

    count = struct.unpack_from("<H", data, 4)[0]

    if count == 0:
        raise ImageError(".ico 里没有任何图像")

    if len(data) < 6 + 16 * count:
        raise ImageError(".ico 目录不完整")

    entries = []

    for index in range(count):
        offset = 6 + 16 * index
        width, height, colors, _reserved, planes, bpp, size, data_offset = (
            struct.unpack_from("<BBBBHHII", data, offset)
        )

        if data_offset + size > len(data):
            raise ImageError(f".ico 第 {index} 个条目的数据超出文件范围")

        payload = data[data_offset:data_offset + size]
        kind = "png" if payload.startswith(PNG_SIGNATURE) else "bmp"

        entries.append(
            {
                "width": width or 256,
                "height": height or 256,
                "colors": colors,
                "planes": planes,
                "bpp": bpp,
                "bytes": size,
                "offset": data_offset,
                "format": kind,
            }
        )

    return entries


# ============================================================================
#  PNG 解码
# ============================================================================

def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)

    if pa <= pb and pa <= pc:
        return a

    if pb <= pc:
        return b

    return c


def _unfilter(raw, width, height, bits_per_pixel):
    """按 PNG 规范还原扫描线（filter 0-4）。"""
    stride = (width * bits_per_pixel + 7) // 8
    bpp = max(1, (bits_per_pixel + 7) // 8)

    if len(raw) < height * (stride + 1):
        raise ImageError("PNG 像素数据不完整（文件被截断？）")

    out = bytearray(height * stride)
    previous = bytearray(stride)
    position = 0

    for row in range(height):
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position:position + stride])
        position += stride

        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up_left = previous[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, previous[i], up_left)) & 0xFF
        else:
            raise ImageError(f"PNG 里出现了未知的 filter 类型 {filter_type}")

        out[row * stride:(row + 1) * stride] = line
        previous = line

    return bytes(out), stride


def _row_samples(row, count, depth):
    """把一行字节拆成样本值（低位深度的位打包，MSB 在前）。"""
    if depth == 8:
        return list(row[:count])

    if depth == 16:
        return [
            (row[2 * i] << 8) | row[2 * i + 1]
            for i in range(count)
        ]

    samples = []
    per_byte = 8 // depth
    mask = (1 << depth) - 1

    for index in range(count):
        byte = row[index // per_byte]
        shift = 8 - depth * (index % per_byte + 1)
        samples.append((byte >> shift) & mask)

    return samples


def decode_png(path):
    """
    读 PNG，返回 `(width, height, rgba_bytes)`（RGBA，8 位/通道）。

    不支持的变体抛 `ImageError`，消息里写清楚"怎么办"。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ImageError(f"读不到图片：{exc}")

    if not data.startswith(PNG_SIGNATURE):
        raise ImageError("不是 PNG 文件")

    position = 8
    header = None
    palette = b""
    transparency = b""
    idat = bytearray()
    seen_end = False

    while position + 8 <= len(data):
        length, chunk_type = struct.unpack_from(">I4s", data, position)
        position += 8
        payload = data[position:position + length]
        position += length + 4          # 跳过 CRC

        if chunk_type == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload[:13])
        elif chunk_type == b"PLTE":
            palette = payload
        elif chunk_type == b"tRNS":
            transparency = payload
        elif chunk_type == b"IDAT":
            idat += payload
        elif chunk_type == b"IEND":
            seen_end = True
            break

    if header is None:
        raise ImageError("PNG 里没有 IHDR")

    if not seen_end and not idat:
        raise ImageError("PNG 里没有像素数据")

    width, height, depth, color_type, compression, filter_method, interlace = header

    if width <= 0 or height <= 0:
        raise ImageError("PNG 尺寸不合法")

    if compression != 0 or filter_method != 0:
        raise ImageError("PNG 用了不支持的压缩/过滤方式")

    if interlace != 0:
        raise ImageError("不支持隔行（Adam7）PNG，请另存为非隔行 PNG 或直接提供 .ico")

    if color_type not in _CHANNELS:
        raise ImageError(f"不支持的 PNG 颜色类型 {color_type}")

    if color_type in (0, 3) and depth not in (1, 2, 4, 8, 16):
        raise ImageError(f"不支持的 PNG 位深 {depth}")

    if color_type in (2, 4, 6) and depth not in (8, 16):
        raise ImageError(
            f"PNG 颜色类型 {color_type} 只支持 8/16 位深，收到 {depth}"
        )

    if color_type == 3 and not palette:
        raise ImageError("调色板 PNG 缺 PLTE")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError(f"PNG 像素数据解压失败：{exc}")

    channels = _CHANNELS[color_type]
    bits_per_pixel = channels * depth
    pixels, stride = _unfilter(raw, width, height, bits_per_pixel)

    palette_alpha = transparency if color_type == 3 else b""
    transparent_gray = None
    transparent_rgb = None

    if color_type == 0 and len(transparency) >= 2:
        transparent_gray = struct.unpack(">H", transparency[:2])[0]
    elif color_type == 2 and len(transparency) >= 6:
        transparent_rgb = struct.unpack(">HHH", transparency[:6])

    rgba = bytearray(width * height * 4)
    out = 0

    for row in range(height):
        line = pixels[row * stride:(row + 1) * stride]
        samples = _row_samples(line, width * channels, depth)

        for column in range(width):
            base = column * channels

            if color_type == 0:
                value = samples[base]
                alpha = 255

                if transparent_gray is not None and value == transparent_gray:
                    alpha = 0

                if depth == 16:
                    level = value >> 8
                elif depth == 8:
                    level = value
                else:
                    level = value * 255 // ((1 << depth) - 1)

                rgba[out:out + 4] = bytes((level, level, level, alpha))
            elif color_type == 2:
                if depth == 16:
                    red = samples[base] >> 8
                    green = samples[base + 1] >> 8
                    blue = samples[base + 2] >> 8
                else:
                    red, green, blue = samples[base], samples[base + 1], samples[base + 2]

                alpha = 255

                if transparent_rgb is not None:
                    if depth == 8:
                        compare = (red, green, blue)
                        target = tuple(value & 0xFF for value in transparent_rgb)
                    else:
                        compare = (samples[base], samples[base + 1], samples[base + 2])
                        target = transparent_rgb

                    if compare == target:
                        alpha = 0

                rgba[out:out + 4] = bytes((red, green, blue, alpha))
            elif color_type == 3:
                index = samples[base]
                pointer = index * 3

                if pointer + 3 > len(palette):
                    raise ImageError("调色板 PNG 的索引超出 PLTE 范围")

                red, green, blue = palette[pointer:pointer + 3]
                alpha = (
                    palette_alpha[index]
                    if index < len(palette_alpha)
                    else 255
                )
                rgba[out:out + 4] = bytes((red, green, blue, alpha))
            elif color_type == 4:
                level = samples[base] >> 8 if depth == 16 else samples[base]
                alpha = samples[base + 1] >> 8 if depth == 16 else samples[base + 1]
                rgba[out:out + 4] = bytes((level, level, level, alpha))
            else:                      # color_type == 6
                if depth == 16:
                    red = samples[base] >> 8
                    green = samples[base + 1] >> 8
                    blue = samples[base + 2] >> 8
                    alpha = samples[base + 3] >> 8
                else:
                    red, green, blue, alpha = (
                        samples[base],
                        samples[base + 1],
                        samples[base + 2],
                        samples[base + 3],
                    )

                rgba[out:out + 4] = bytes((red, green, blue, alpha))

            out += 4

    return width, height, bytes(rgba)


# ============================================================================
#  缩放 + 编码 ICO
# ============================================================================

def resize_rgba(rgba, width, height, new_width, new_height):
    """
    盒式滤波缩放（面积平均）—— 对缩小的图标来说质量足够，且完全可预测。

    放大会用最近邻（图标场景里几乎不会用到）。
    """
    if new_width == width and new_height == height:
        return bytes(rgba)

    result = bytearray(new_width * new_height * 4)

    for y in range(new_height):
        y0 = y * height // new_height
        y1 = max(y0 + 1, (y + 1) * height // new_height)

        for x in range(new_width):
            x0 = x * width // new_width
            x1 = max(x0 + 1, (x + 1) * width // new_width)
            red = green = blue = alpha = 0
            count = 0

            for sy in range(y0, min(y1, height)):
                row = sy * width

                for sx in range(x0, min(x1, width)):
                    pointer = (row + sx) * 4
                    red += rgba[pointer]
                    green += rgba[pointer + 1]
                    blue += rgba[pointer + 2]
                    alpha += rgba[pointer + 3]
                    count += 1

            if count == 0:
                count = 1

            pointer = (y * new_width + x) * 4
            result[pointer] = red // count
            result[pointer + 1] = green // count
            result[pointer + 2] = blue // count
            result[pointer + 3] = alpha // count

    return bytes(result)


def _ico_bitmap_entry(rgba, size):
    """一个尺寸的 ICO 数据块：BITMAPINFOHEADER + BGRA（自下而上）+ AND 掩码。"""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,             # biSize
        size,           # biWidth
        size * 2,       # biHeight = XOR + AND 两张图
        1,              # biPlanes
        32,             # biBitCount
        0,              # biCompression = BI_RGB
        size * size * 4,
        0, 0, 0, 0,
    )

    xor = bytearray(size * size * 4)
    mask_stride = ((size + 31) // 32) * 4
    and_mask = bytearray(mask_stride * size)

    for y in range(size):
        source_row = (size - 1 - y) * size * 4       # ICO 是自下而上
        target_row = y * size * 4

        for x in range(size):
            pointer = source_row + x * 4
            red = rgba[pointer]
            green = rgba[pointer + 1]
            blue = rgba[pointer + 2]
            alpha = rgba[pointer + 3]
            target = target_row + x * 4
            xor[target] = blue
            xor[target + 1] = green
            xor[target + 2] = red
            xor[target + 3] = alpha

            if alpha < 128:
                # AND 掩码：1 = 透明（老 API 不看 alpha 时用）
                and_mask[y * mask_stride + (x // 8)] |= 0x80 >> (x % 8)

    return header + bytes(xor) + bytes(and_mask)


def encode_ico(images):
    """
    把 `[(size, rgba_bytes), ...]` 编码成 .ico 字节。

    条目顺序按尺寸从小到大（Windows 习惯）。
    """
    entries = sorted(images, key=lambda item: item[0])

    if not entries:
        raise ImageError("没有可写入的图标尺寸")

    payloads = [
        (size, _ico_bitmap_entry(rgba, size)) for size, rgba in entries
    ]

    offset = 6 + 16 * len(payloads)
    directory = bytearray()
    body = bytearray()

    for size, payload in payloads:
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,     # bWidth（0 = 256）
            size if size < 256 else 0,     # bHeight
            0,                             # bColorCount（32 位图固定 0）
            0,                             # bReserved（必须为 0）
            1,                             # wPlanes
            32,                            # wBitCount
            len(payload),
            offset,
        )
        body += payload
        offset += len(payload)

    return ICO_SIGNATURE[:4] + struct.pack("<H", len(payloads)) + bytes(directory) + bytes(body)


def png_to_ico_bytes(path, sizes=DEFAULT_ICO_SIZES):
    """把一张 PNG 变成 .ico 字节（多尺寸）。"""
    width, height, rgba = decode_png(path)

    images = []

    for size in sizes:
        if size > max(width, height):
            continue

        images.append((size, resize_rgba(rgba, width, height, size, size)))

    if not images:
        # 源图比所有目标尺寸都小：至少保留它自己的尺寸，别产出空图标。
        side = max(width, height)
        images.append((side, resize_rgba(rgba, width, height, side, side)))

    return encode_ico(images)


def write_ico_from_image(source, target, sizes=DEFAULT_ICO_SIZES):
    """
    写 `.ico`：

      * 源文件已经是 `.ico` -> 原样复制（不重新编码，避免二次损失）；
      * 源文件是 PNG       -> 解码 + 缩放 + 编码成多尺寸 ico。

    返回 target 路径；失败抛 `ImageError`。
    """
    if not os.path.isfile(source):
        raise ImageError(f"图片不存在：{source}")

    target_dir = os.path.dirname(os.path.abspath(target))

    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    if source.lower().endswith(".ico"):
        ico_entries(source)          # 顺带校验一下，坏图标不要静默带进安装器
        with open(source, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())

        return target

    payload = png_to_ico_bytes(source, sizes)

    with open(target, "wb") as fh:
        fh.write(payload)

    return target
