// 皮肤部署套件·图像处理：去纯色背景(边缘洪泛) → 裁切 → 等比放入 200x250 画布(底部对齐) → 输出 2x PNG
// 用法: skinkit <输入图片> <输出.png> [容差=38] [裁切 x,y,w,h 均为 0~1 比例] [split=N 按空白间隙自动切出 N 个角色，输出 <输出>_i.png]
import AppKit
let a = CommandLine.arguments
guard a.count >= 3, let src = NSImage(contentsOfFile: a[1]),
      var cg = src.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print("无法读取图片"); exit(1) }
if a.count > 4 {
    let f = a[4].split(separator: ",").compactMap { Double($0) }
    if f.count == 4, let c = cg.cropping(to: CGRect(x: f[0] * Double(cg.width), y: f[1] * Double(cg.height),
                                                    width: f[2] * Double(cg.width), height: f[3] * Double(cg.height))) { cg = c }
}
let tol = a.count > 3 ? Double(a[3]) ?? 38 : 38
let w = cg.width, h = cg.height
var px = [UInt8](repeating: 0, count: w * h * 4)
let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
// 背景色 = 边缘像素（内缩 3px，避开网页图常见的描边/杂线）的众数，量化到 16 级
var hist: [Int: Int] = [:], border: [Int] = []
for x in 3..<max(4, w - 3) { border += [3 * w + x, (h - 4) * w + x] }
for y in 3..<max(4, h - 3) { border += [y * w + 3, y * w + w - 4] }
for i in border { let k = Int(px[i * 4]) / 16 << 8 | Int(px[i * 4 + 1]) / 16 << 4 | Int(px[i * 4 + 2]) / 16; hist[k, default: 0] += 1 }
let mode = hist.max { $0.value < $1.value }!.key
var bg = [0.0, 0.0, 0.0], nbg = 0.0
for i in border { let k = Int(px[i * 4]) / 16 << 8 | Int(px[i * 4 + 1]) / 16 << 4 | Int(px[i * 4 + 2]) / 16
    if k == mode { for c in 0..<3 { bg[c] += Double(px[i * 4 + c]) }; nbg += 1 } }
bg = bg.map { $0 / max(nbg, 1) }
// 绿幕/彩色背景：可放心去除封闭区域，且不做白底阴影规则；白底则收紧阈值保护白色衣物
let bgSat = bg.max()! - bg.min()!, chroma = bgSat > 80
let greenScreen = chroma && bg[1] > bg[0] + 40 && bg[1] > bg[2] + 40
// 最外 2px 边框直接透明
for y in 0..<h { for x in 0..<w where x < 2 || y < 2 || x >= w - 2 || y >= h - 2 { for k in 0..<4 { px[(y * w + x) * 4 + k] = 0 } } }
func dist(_ i: Int) -> Double {
    let r = Double(px[i * 4]) - bg[0], g = Double(px[i * 4 + 1]) - bg[1], b = Double(px[i * 4 + 2]) - bg[2]
    return (r * r + g * g + b * b).squareRoot()
}
var seen = [Bool](repeating: false, count: w * h), stack: [Int] = []
for x in 2..<max(3, w - 2) { stack += [2 * w + x, (h - 3) * w + x] }
for y in 2..<max(3, h - 2) { stack += [y * w + 2, y * w + w - 3] }
let alreadyTransparent = px[(3 * w + 3) * 4 + 3] < 10 && px[((h - 4) * w + w - 4) * 4 + 3] < 10
if nbg == 0 || alreadyTransparent { stack = [] }  // 源图已是透明背景：不做任何去背景处理
while let i = stack.popLast() {
    if seen[i] { continue }
    seen[i] = true
    let d = dist(i)
    if d > tol * 1.8 { continue }
    let alpha = d <= tol ? 0.0 : (d - tol) / (tol * 0.8)  // 边缘羽化
    for k in 0..<4 { px[i * 4 + k] = UInt8(Double(px[i * 4 + k]) * alpha) }
    if d > tol { continue }
    let x = i % w, y = i / w
    if x > 0 { stack.append(i - 1) }; if x < w - 1 { stack.append(i + 1) }
    if y > 0 { stack.append(i - w) }; if y < h - 1 { stack.append(i + w) }
}
// 第二遍：身体下半部被腿/阴影围住的背景洞（胯下、腿间），以及脚底的灰色地面阴影
if !alreadyTransparent {
    var minY = h, maxY = 0
    for y in 0..<h { for x in 0..<w where px[(y * w + x) * 4 + 3] > 16 { minY = min(minY, y); maxY = max(maxY, y); break } }
    if maxY > minY {
        let lower = minY + Int(Double(maxY - minY) * 0.45)      // 只处理下半身，避免误删牙齿、眼白
        var mark = [Bool](repeating: false, count: w * h)
        for y0 in lower..<h { for x0 in 0..<w {
            let i0 = y0 * w + x0
            let holeTol = chroma ? tol : tol * 0.5
            if mark[i0] || px[i0 * 4 + 3] < 16 || dist(i0) > holeTol { continue }
            var comp: [Int] = [], st = [i0]; mark[i0] = true
            while let i = st.popLast() {
                comp.append(i)
                let x = i % w, y = i / w
                for (nx, ny) in [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)] where nx >= 0 && ny >= 0 && nx < w && ny < h {
                    let j = ny * w + nx
                    if !mark[j] && px[j * 4 + 3] >= 16 && dist(j) <= holeTol { mark[j] = true; st.append(j) }
                }
            }
            if comp.count >= 30 { for i in comp { for k in 0..<4 { px[i * 4 + k] = 0 } } }
        } }
        let band = maxY - Int(Double(maxY - minY) * 0.14)       // 脚底带：去低饱和、偏亮的灰色阴影（仅白/浅灰底）
        let bgLuma = (bg[0] * 3 + bg[1] * 6 + bg[2]) / 10
        if !chroma && bgLuma > 200 { for y in max(0, band)..<h { for x in 0..<w {
            let i = (y * w + x) * 4
            guard px[i + 3] > 0 else { continue }
            let r = Int(px[i]), g = Int(px[i + 1]), b = Int(px[i + 2])
            let sat = max(r, g, b) - min(r, g, b), luma = (r * 3 + g * 6 + b) / 10
            if (sat < 22 && luma > 135) || (sat < 50 && luma > 170) { for k in 0..<4 { px[i + k] = 0 } }
        } } }
        if greenScreen && !alreadyTransparent {  // 去绿色溢边：绿分量不超过红/蓝中的较大者
            for i in stride(from: 0, to: w * h * 4, by: 4) where px[i + 3] > 0 {
                let m = max(px[i], px[i + 2]); if px[i + 1] > m { px[i + 1] = m }
            }
        }
    }
}
let full = ctx.makeImage()!
// 按列占用找角色：连续空白列(≥4)视为间隙，取最宽的 N 段
func segments() -> [(Int, Int)] {
    var occ = [Bool](repeating: false, count: w)
    for x in 0..<w { var n = 0; for y in 0..<h where px[(y * w + x) * 4 + 3] > 16 { n += 1; if n > 2 { occ[x] = true; break } } }
    var segs: [(Int, Int)] = [], start = -1, gap = 0
    for x in 0..<w {
        if occ[x] { if start < 0 { start = x }; gap = 0 }
        else if start >= 0 { gap += 1; if gap >= 4 { segs.append((start, x - gap)); start = -1; gap = 0 } }
    }
    if start >= 0 { segs.append((start, w - 1)) }
    return segs
}
func bbox(_ x0: Int, _ x1: Int) -> CGRect? {
    var minX = w, minY = h, maxX = 0, maxY = 0
    for y in 0..<h { for x in x0...x1 where px[(y * w + x) * 4 + 3] > 16 { minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y) } }
    return maxX > minX ? CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1) : nil
}
let splitN = a.count > 5 && a[5].hasPrefix("split=") ? Int(a[5].dropFirst(6)) ?? 0 : 0
var cuts: [CGImage] = []
if splitN > 0 {
    let segs = segments().sorted { $0.1 - $0.0 > $1.1 - $1.0 }.prefix(splitN).sorted { $0.0 < $1.0 }
    guard segs.count == splitN else { print("只识别到 \(segs.count) 个角色，期望 \(splitN)"); exit(2) }
    for (x0, x1) in segs { if let r = bbox(x0, x1) { cuts.append(full.cropping(to: r)!) } }
} else if ProcessInfo.processInfo.environment["SKINKIT_KEEPFRAME"] == "1" {
    cuts = [full]  // 保留原构图不裁切：模块化衣橱的身体图与衣物叠加图必须同构图才能对齐
} else {
    guard let r = bbox(0, w - 1) else { print("未找到主体"); exit(1) }
    cuts = [full.cropping(to: r)!]
}
// 多个角色时按最高者统一缩放，保持相对大小一致
let maxH = cuts.map { $0.height }.max()!, maxW = cuts.map { $0.width }.max()!
let sz = (ProcessInfo.processInfo.environment["SKINKIT_SIZE"] ?? "400x500").split(separator: "x").compactMap { Int($0) }
let OW = sz.count == 2 ? sz[0] : 400, OH = sz.count == 2 ? sz[1] : 500, pad = 8
for (i, cut) in cuts.enumerated() {
let s = min(Double(OW - 2 * pad) / Double(maxW), Double(OH - 2 * pad) / Double(maxH))
let dw = Double(cut.width) * s, dh = Double(cut.height) * s
let out = CGContext(data: nil, width: OW, height: OH, bitsPerComponent: 8, bytesPerRow: 0,
                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
out.interpolationQuality = .high
out.draw(cut, in: CGRect(x: (Double(OW) - dw) / 2, y: Double(pad), width: dw, height: dh))
let rep = NSBitmapImageRep(cgImage: out.makeImage()!)
let path = splitN > 0 ? a[2].replacingOccurrences(of: ".png", with: "_\(i).png") : a[2]
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
print("ok \(Int(dw))x\(Int(dh))")
}
