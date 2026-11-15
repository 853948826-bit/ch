// 赛博诊所 · 露米（Lumi）桌面小精灵
// 分层矢量 + Core Animation（动画在系统渲染服务执行，几乎不占 CPU）。皮肤由守护进程 /skin 下发，版本号变化即热更新。
// 手势（与常见桌宠一致，降低迁移成本）：戳一戳=随机反应  长按=摸头  双击=勿扰/静音  右键=菜单  拖动=拎起
// 唤醒：⌃⌥Space 轻按=打字提需求，按住=说话（松开发送）；可选常驻唤醒词“露米”。全屏/游戏时自动勿扰。
import AppKit
import AVFoundation
import Carbon.HIToolbox
import Speech

let API = ProcessInfo.processInfo.environment["CLINIC_URL"] ?? "http://127.0.0.1:8765"
var W: CGFloat = 200, H: CGFloat = 250  // 画布尺寸跟随皮肤 viewBox

// MARK: - 网络
enum Net {
    /// 本机接口令牌（守护进程首次启动生成于 ~/.cyberclinic/token，仅当前用户可读）
    static var token: String { (try? String(contentsOfFile: NSHomeDirectory() + "/.cyberclinic/token", encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "" }
    static func get(_ path: String, timeout: TimeInterval = 5) -> [String: Any]? {
        guard let url = URL(string: API + path) else { return nil }
        var req = URLRequest(url: url); req.timeoutInterval = timeout
        req.setValue(token, forHTTPHeaderField: "X-Clinic-Token")
        var out: [String: Any]?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { d, _, _ in
            if let d = d { out = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] }
            sem.signal()
        }.resume()
        sem.wait()
        return out
    }
    static func post(_ path: String, _ body: [String: Any], done: (([String: Any]?) -> Void)? = nil) {
        var req = URLRequest(url: URL(string: API + path)!)
        req.httpMethod = "POST"; req.timeoutInterval = 15
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(token, forHTTPHeaderField: "X-Clinic-Token")
        URLSession.shared.dataTask(with: req) { d, _, _ in
            let obj = d.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
            DispatchQueue.main.async { done?(obj) }
        }.resume()
    }
}

// MARK: - 皮肤
struct SkinData {
    var ver = 0, images: [String: CGImage] = [:], anchors: [String: CGPoint] = [:]
    var lines: [String: [String]] = [:], accent = NSColor.systemTeal, motions: [String: [[String]]] = [:]
    var showLED = true, eyeTrack = true, sounds: [String: String] = [:], whole: Set<String> = []
    var size = CGSize(width: 200, height: 250), tts = false
    var interactionZones: [String: [Double]] = [:], interactionPoses: [String: String] = [:], gazePoses: [String: String] = [:]
    var interactionVariants: [String: [String]] = [:]
    var poseInteractionZones: [String: [String: [Double]]] = [:]
    var sharedPoses: Set<String> = []

    static func load(_ obj: [String: Any]) -> SkinData {
        var s = SkinData()
        s.ver = obj["ver"] as? Int ?? 0
        if let vb = obj["viewBox"] as? [Double], vb.count == 2, vb[0] > 0, vb[1] > 0 { s.size = CGSize(width: vb[0], height: vb[1]) }
        for (k, v) in obj["layers"] as? [String: String] ?? [:] {
            guard let img = NSImage(data: Data(v.utf8)) else { continue }
            var r = NSRect(x: 0, y: 0, width: s.size.width * 2, height: s.size.height * 2)
            s.images[k] = img.cgImage(forProposedRect: &r, context: nil, hints: nil)
        }
        for (k, v) in obj["anchors"] as? [String: [Double]] ?? [:] where v.count == 2 {
            s.anchors[k] = CGPoint(x: v[0], y: v[1])
        }
        s.lines = obj["lines"] as? [String: [String]] ?? [:]
        s.motions = obj["motions"] as? [String: [[String]]] ?? [:]
        s.showLED = obj["show_led"] as? Bool ?? true; s.eyeTrack = obj["eye_track"] as? Bool ?? true
        s.sounds = obj["sounds"] as? [String: String] ?? [:]
        s.whole = Set(obj["whole_states"] as? [String] ?? [])
        s.tts = obj["tts"] as? Bool ?? false
        s.interactionZones = obj["interaction_zones"] as? [String: [Double]] ?? [:]
        s.interactionPoses = obj["interaction_poses"] as? [String: String] ?? [:]
        s.interactionVariants = obj["interaction_variants"] as? [String: [String]] ?? [:]
        s.poseInteractionZones = obj["pose_interaction_zones"] as? [String: [String: [Double]]] ?? [:]
        s.gazePoses = obj["gaze_poses"] as? [String: String] ?? [:]
        s.sharedPoses = Set(obj["shared_pose_names"] as? [String] ?? [])
        if let hex = (obj["palette"] as? [String: String])?["accent"] { s.accent = NSColor(hex: hex) ?? s.accent }
        return s
    }
    func line(_ key: String, _ fallback: String = "") -> String { lines[key]?.randomElement() ?? fallback }
}

extension NSColor {
    convenience init?(hex: String) {
        var h = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        if h.count == 3 { h = h.map { "\($0)\($0)" }.joined() }
        guard h.count >= 6, let v = UInt32(h.prefix(6), radix: 16) else { return nil }
        self.init(srgbRed: CGFloat(v >> 16 & 255) / 255, green: CGFloat(v >> 8 & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: 1)
    }
}

// MARK: - 角色舞台
enum Mood { case normal, happy, worried, alert, sleep, listening, thinking, dragging }

final class Stage: NSView {
    let stage = CALayer(), root = CALayer(), shade = CALayer(), head = CALayer(), led = CALayer(), pose = CALayer()
    var poseGen = 0
    var activePose: String?
    var L: [String: CALayer] = [:]
    var skin = SkinData(), eyes = "eyes_open", mouth = "mouth_smile"
    var onDown: ((NSEvent) -> Void)?, onDrag: ((NSEvent) -> Void)?, onUp: ((NSEvent) -> Void)?, onRight: ((NSEvent) -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = .clear
        stage.bounds = CGRect(x: 0, y: 0, width: W, height: H)
        layer?.addSublayer(stage)
        shade.frame = CGRect(x: 62, y: 4, width: 76, height: 12)
        shade.backgroundColor = NSColor.black.withAlphaComponent(0.16).cgColor
        shade.cornerRadius = 6
        stage.addSublayer(shade)
        root.frame = stage.bounds
        stage.addSublayer(root)
        for n in ["hair_back", "body", "arm_l", "arm_r", "holo"] { L[n] = add(n, to: root) }
        head.frame = stage.bounds
        root.insertSublayer(head, above: L["arm_r"])
        for n in ["face", "blush", "eyes", "mouth", "braid", "bangs", "ahoge", "cap"] { L[n] = add(n, to: head) }
        pose.frame = stage.bounds; pose.contentsGravity = .resize; pose.isHidden = true
        root.addSublayer(pose)
        root.addSublayer(L["holo"]!)  // 全息板在最上层
        led.bounds = CGRect(x: 0, y: 0, width: 9, height: 9); led.cornerRadius = 4.5
        led.borderWidth = 1.5; led.borderColor = NSColor(white: 0.25, alpha: 1).cgColor
        head.addSublayer(led)
        L["holo"]!.opacity = 0
        L["blush"]!.opacity = 0.35
        layout(scale: 0.72)
    }
    required init?(coder: NSCoder) { fatalError() }

    func add(_ n: String, to p: CALayer) -> CALayer {
        let l = CALayer(); l.frame = stage.bounds; l.contentsGravity = .resize; l.name = n
        p.addSublayer(l); return l
    }

    func layout(scale s: CGFloat) {
        stage.setAffineTransform(.identity)
        stage.position = CGPoint(x: W * s / 2, y: H * s / 2)
        stage.setAffineTransform(CGAffineTransform(scaleX: s, y: s))
    }

    /// 设置旋转支点：SVG 坐标(y 向下) → 图层坐标(y 向上)
    func pivot(_ l: CALayer, _ p: CGPoint) {
        l.anchorPoint = CGPoint(x: p.x / W, y: 1 - p.y / H)
        l.position = CGPoint(x: p.x, y: H - p.y)
    }

    func apply(_ s: SkinData) {
        clearPose()
        skin = s
        CATransaction.begin(); CATransaction.setDisableActions(true)
        W = s.size.width; H = s.size.height
        stage.bounds = CGRect(x: 0, y: 0, width: W, height: H)
        let foot = s.anchors["body"]?.y ?? (H - 10)  // 影子贴住脚底
        shade.frame = CGRect(x: W / 2 - 30, y: max(2, H - foot - 5), width: 60, height: 10)
        for l in [root, head, pose] + Array(L.values) { l.anchorPoint = CGPoint(x: 0.5, y: 0.5); l.frame = stage.bounds }
        for n in ["hair_back", "body", "arm_l", "arm_r", "holo", "face", "blush", "bangs", "cap", "ahoge", "braid"] { L[n]?.contents = s.images[n] }
        pivot(L["ahoge"]!, s.anchors["ahoge"] ?? CGPoint(x: 100, y: 40))
        pivot(L["braid"]!, s.anchors["braid"] ?? CGPoint(x: 150, y: 100))
        pivot(head, s.anchors["head"] ?? CGPoint(x: 100, y: 140))
        pivot(L["hair_back"]!, s.anchors["head"] ?? CGPoint(x: 100, y: 140))
        pivot(L["arm_l"]!, s.anchors["arm_l"] ?? CGPoint(x: 80, y: 150))
        pivot(L["arm_r"]!, s.anchors["arm_r"] ?? CGPoint(x: 120, y: 150))
        pivot(L["body"]!, s.anchors["body"] ?? CGPoint(x: 100, y: 240))
        let lp = s.anchors["led"] ?? CGPoint(x: 134, y: 32)
        led.position = CGPoint(x: lp.x, y: H - lp.y); led.isHidden = !s.showLED
        L["eyes"]?.position = CGPoint(x: W / 2, y: H / 2)
        setFace(eyes, mouth)
        CATransaction.commit()
        startLoops()
    }

    func setFace(_ e: String? = nil, _ m: String? = nil) {
        CATransaction.begin(); CATransaction.setDisableActions(true)
        if let e = e {
            eyes = e; L["eyes"]?.contents = skin.images[e] ?? skin.images["eyes_open"]
            L["body"]?.isHidden = skin.whole.contains(e)  // 整图状态：替换而非叠加本体
        }
        if let m = m { mouth = m; L["mouth"]?.contents = skin.images[m] ?? skin.images["mouth_smile"] }
        CATransaction.commit()
    }

    func startLoops() {
        root.removeAnimation(forKey: "float"); L["body"]?.removeAnimation(forKey: "breath")
        let f = CABasicAnimation(keyPath: "transform.translation.y")
        f.fromValue = 0; f.toValue = 4; f.duration = 1.7; f.autoreverses = true; f.repeatCount = .infinity
        f.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        root.add(f, forKey: "float")
        let sh = CABasicAnimation(keyPath: "transform.scale.x")
        sh.fromValue = 1; sh.toValue = 0.88; sh.duration = 1.7; sh.autoreverses = true; sh.repeatCount = .infinity
        shade.add(sh, forKey: "float")
        let b = CABasicAnimation(keyPath: "transform.scale.y")
        b.fromValue = 1; b.toValue = 1.018; b.duration = 2.1; b.autoreverses = true; b.repeatCount = .infinity
        L["body"]?.add(b, forKey: "breath")
        // 呆毛与发辫持续轻摆（渲染服务执行，几乎不占 CPU）
        for (n, amp, d) in [("ahoge", 0.14, 1.3), ("braid", 0.07, 1.9)] {
            let w = CABasicAnimation(keyPath: "transform.rotation.z")
            w.fromValue = -amp; w.toValue = amp; w.duration = d; w.autoreverses = true; w.repeatCount = .infinity
            w.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            L[n]?.add(w, forKey: "sway")
        }
    }
    /// 呆毛弹一下（跳跃、被戳时）
    func boing() {
        let a = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        a.values = [0, 0.5, -0.35, 0.2, -0.1, 0]; a.duration = 0.8; a.isAdditive = true
        L["ahoge"]?.add(a, forKey: "boing")
    }

    func setFloat(speed: Float) { root.speed = speed; shade.speed = speed; L["body"]?.speed = speed }

    func rotate(_ l: CALayer, _ values: [Double], _ dur: Double, key: String) {
        let a = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        a.values = values; a.duration = dur; a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        l.add(a, forKey: key)
    }
    func tilt(_ v: [Double], _ d: Double) { rotate(head, v, d, key: "tilt"); rotate(L["hair_back"]!, v, d, key: "tilt") }
    func wave() { rotate(L["arm_r"]!, [0, 2.5, 2.1, 2.5, 2.1, 2.5, 0], 1.8, key: "wave"); tilt([0, -0.08, -0.08, 0], 1.8) }
    func stretch() {
        rotate(L["arm_l"]!, [0, -2.6, -2.6, 0], 2.0, key: "s"); rotate(L["arm_r"]!, [0, 2.6, 2.6, 0], 2.0, key: "s")
        hop(height: 4, dur: 2.0)
    }
    func hop(height: Double = 16, dur: Double = 0.45) {
        let a = CAKeyframeAnimation(keyPath: "transform.translation.y")
        a.values = [0, height, 0, height * 0.25, 0]; a.keyTimes = [0, 0.4, 0.75, 0.88, 1]; a.duration = dur
        stage.add(a, forKey: "hop"); boing()
    }
    func squash() {
        let a = CAKeyframeAnimation(keyPath: "transform.scale.y")
        a.values = [1, 0.86, 1.06, 1]; a.duration = 0.35
        L["body"]?.add(a, forKey: "squash")
    }
    func jiggle() {
        let a = CAKeyframeAnimation(keyPath: "transform.scale.x"), b = CAKeyframeAnimation(keyPath: "transform.scale.y")
        a.values = [1, 1.08, 0.94, 1.04, 0.98, 1]; b.values = [1, 0.92, 1.06, 0.97, 1.02, 1]
        for k in [a, b] { k.duration = 0.7; k.timingFunction = CAMediaTimingFunction(name: .easeOut) }
        let g = CAAnimationGroup(); g.animations = [a, b]; g.duration = 0.7
        L["body"]?.add(g, forKey: "jiggle"); head.add(g, forKey: "jiggle")
    }
    func spin() {
        let a = CABasicAnimation(keyPath: "transform.rotation.y"); a.fromValue = 0; a.toValue = Double.pi * 2
        a.duration = 0.9; a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        root.add(a, forKey: "spin")
    }
    func rub() {
        rotate(L["arm_l"]!, [0, -0.22, 0.08, -0.22, 0.08, 0], 1.5, key: "rub")
        rotate(L["arm_r"]!, [0, 0.22, -0.08, 0.22, -0.08, 0], 1.5, key: "rub")
    }
    func shake(_ dur: Double) {
        let n = max(4, Int(dur * 14))
        let r = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        r.values = (0...n).map { i in i == 0 || i == n ? 0 : (i % 2 == 0 ? 0.05 : -0.05) * (1 - Double(i) / Double(n) * 0.5) }
        r.duration = dur
        let t = CAKeyframeAnimation(keyPath: "transform.translation.y")
        t.values = (0...n).map { i in i % 2 == 0 ? 0 : 2.5 }; t.duration = dur
        stage.add(r, forKey: "shake"); L["body"]?.add(t, forKey: "shakeY")
    }
    func collapse() {
        let a = CAKeyframeAnimation(keyPath: "transform.scale.y")
        a.values = [1, 0.78, 0.8, 0.8, 1.05, 1]; a.keyTimes = [0, 0.15, 0.3, 0.75, 0.9, 1]; a.duration = 2.2
        let b = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        b.values = [0, 0.12, 0.1, 0.1, 0, 0]; b.keyTimes = [0, 0.15, 0.3, 0.75, 0.9, 1]; b.duration = 2.2
        L["body"]?.add(a, forKey: "collapse"); stage.add(b, forKey: "collapse")
    }
    /// 临时切换为整图姿势（如狂笑、挥手），结束后恢复本体
    func showPose(_ name: String, _ secs: Double?) {
        guard let img = skin.images["pose_" + name] else { return }
        if secs == nil && activePose == name { return }
        poseGen += 1; let g = poseGen
        activePose = name
        CATransaction.begin(); CATransaction.setDisableActions(true)
        pose.contents = img; pose.isHidden = false
        for l in [L["body"], L["arm_l"], L["arm_r"], head, L["hair_back"]] { l?.opacity = 0 }
        CATransaction.commit()
        if let secs = secs { DispatchQueue.main.asyncAfter(deadline: .now() + secs) { [weak self] in
            guard let self = self, self.poseGen == g else { return }
            self.clearPose()
        } }
    }
    func clearPose() {
        poseGen += 1; activePose = nil
        CATransaction.begin(); CATransaction.setDisableActions(true)
        pose.isHidden = true; pose.contents = nil
        for l in [L["body"], L["arm_l"], L["arm_r"], head, L["hair_back"]] { l?.opacity = 1 }
        CATransaction.commit()
    }
    func gaze(_ direction: String?) {
        let key = direction.flatMap { skin.gazePoses[$0] }
        CATransaction.begin(); CATransaction.setDisableActions(true)
        L["body"]?.contents = key.flatMap { skin.images["pose_" + $0] } ?? skin.images["body"]
        CATransaction.commit()
    }
    func holo(_ on: Bool) {
        let a = CABasicAnimation(keyPath: "opacity"); a.fromValue = L["holo"]!.opacity; a.toValue = on ? 1 : 0; a.duration = 0.3
        L["holo"]!.opacity = on ? 1 : 0; L["holo"]!.add(a, forKey: "o")
    }
    func blush(_ v: Float) { L["blush"]!.opacity = v }
    func ledColor(_ c: NSColor) { led.backgroundColor = c.cgColor }
    func lookAt(dx: CGFloat, dy: CGFloat) {
        CATransaction.begin(); CATransaction.setAnimationDuration(0.15)
        L["eyes"]?.position = CGPoint(x: W / 2 + dx, y: H / 2 + dy)
        CATransaction.commit()
    }
    func dangle(_ angle: CGFloat) {
        CATransaction.begin(); CATransaction.setAnimationDuration(0.12)
        root.anchorPoint = CGPoint(x: 0.5, y: 0.85); root.position = CGPoint(x: W / 2, y: H * 0.85)
        root.setAffineTransform(CGAffineTransform(rotationAngle: angle))
        CATransaction.commit()
    }
    func particle(_ s: String, dx: CGFloat = 0) {
        let t = CATextLayer()
        t.string = s; t.fontSize = 22; t.alignmentMode = .center; t.contentsScale = 2
        t.frame = CGRect(x: W * 0.6 + dx, y: H * 0.8, width: 30, height: 30)
        root.addSublayer(t)
        let g = CAAnimationGroup()
        let up = CABasicAnimation(keyPath: "position.y"); up.byValue = 34
        let fade = CABasicAnimation(keyPath: "opacity"); fade.fromValue = 1; fade.toValue = 0
        g.animations = [up, fade]; g.duration = 1.6; g.fillMode = .forwards; g.isRemovedOnCompletion = false
        t.add(g, forKey: "p")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.7) { t.removeFromSuperlayer() }
    }

    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseDown(with e: NSEvent) { onDown?(e) }
    override func mouseDragged(with e: NSEvent) { onDrag?(e) }
    override func mouseUp(with e: NSEvent) { onUp?(e) }
    override func rightMouseDown(with e: NSEvent) { onRight?(e) }
}

// MARK: - 气泡
final class KeyPanel: NSPanel { override var canBecomeKey: Bool { true } }

final class Bubble {
    let panel = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 260, height: 90), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    let label = NSTextField(wrappingLabelWithString: ""), input = NSTextField()
    var hideWork: DispatchWorkItem?, onSubmit: ((String) -> Void)?

    init() {
        panel.level = .floating; panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let v = NSView(); v.wantsLayer = true
        v.layer?.backgroundColor = NSColor(white: 1, alpha: 0.97).cgColor
        v.layer?.cornerRadius = 14; v.layer?.borderWidth = 2
        panel.contentView = v
        label.font = .systemFont(ofSize: 12.5); label.textColor = NSColor(white: 0.2, alpha: 1)
        input.placeholderString = "说说哪里不舒服…（回车发送）"; input.isHidden = true; input.focusRingType = .none
        input.target = self; input.action = #selector(send)
        v.addSubview(label); v.addSubview(input)
    }
    func accent(_ c: NSColor) { panel.contentView?.layer?.borderColor = c.cgColor }

    func show(_ text: String, near f: NSRect, hold: TimeInterval = 5, typing: Bool = false) {
        hideWork?.cancel()
        label.stringValue = text
        input.isHidden = !typing
        let w: CGFloat = 270
        let tsize = label.sizeThatFits(NSSize(width: w - 24, height: 400))
        let h = tsize.height + (typing ? 50 : 22)
        label.frame = NSRect(x: 12, y: typing ? 40 : 10, width: w - 24, height: tsize.height + 2)
        input.frame = NSRect(x: 12, y: 10, width: w - 24, height: 24)
        var x = f.minX - w + 30
        if x < (NSScreen.main?.visibleFrame.minX ?? 0) { x = f.maxX - 30 }
        panel.setFrame(NSRect(x: x, y: f.maxY - 18, width: w, height: h), display: true)
        panel.orderFrontRegardless()
        if typing { input.stringValue = ""; panel.makeKey(); panel.makeFirstResponder(input) }
        else if hold > 0 {
            let w = DispatchWorkItem { [weak self] in self?.panel.orderOut(nil) }
            hideWork = w; DispatchQueue.main.asyncAfter(deadline: .now() + hold, execute: w)
        }
    }
    @objc func send() {
        let t = input.stringValue.trimmingCharacters(in: .whitespaces)
        if !t.isEmpty { onSubmit?(t) }
    }
    func hide() { panel.orderOut(nil) }
}

// MARK: - 悬停预览（换装/衣橱菜单）
final class PreviewPanel {
    let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 190, height: 330), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    let image = NSImageView(), label = NSTextField(labelWithString: "")
    var cache: [String: NSImage] = [:]
    init() {
        panel.level = .popUpMenu; panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        let v = NSView(); v.wantsLayer = true
        v.layer?.backgroundColor = NSColor(white: 1, alpha: 0.97).cgColor; v.layer?.cornerRadius = 12
        panel.contentView = v
        image.imageScaling = .scaleProportionallyUpOrDown
        image.frame = NSRect(x: 10, y: 34, width: 170, height: 286)
        label.frame = NSRect(x: 8, y: 6, width: 174, height: 24); label.alignment = .center
        label.font = .systemFont(ofSize: 11.5); label.textColor = NSColor(white: 0.25, alpha: 1)
        v.addSubview(image); v.addSubview(label)
    }
    /// key 形如 pack=<id> 或 item=<id>；预览 SVG 由守护进程合成，不改变当前皮肤
    func show(key: String, title: String, at p: NSPoint) {
        if cache[key] == nil, let r = Net.get("/preview?" + key, timeout: 3), let svg = r["svg"] as? String {
            cache[key] = NSImage(data: Data(svg.utf8))
        }
        guard let img = cache[key] else { hide(); return }
        image.image = img; label.stringValue = title
        var x = p.x - panel.frame.width - 16
        if let sf = NSScreen.main?.visibleFrame, x < sf.minX { x = p.x + 260 }
        panel.setFrameOrigin(NSPoint(x: x, y: p.y - panel.frame.height / 2))
        panel.orderFrontRegardless()
    }
    func hide() { panel.orderOut(nil) }
}

// MARK: - 语音：按住说话 + 可选唤醒词（端侧识别，不上传音频）
final class Listener {
    let rec = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
    var engine = AVAudioEngine(), req: SFSpeechAudioBufferRecognitionRequest?, task: SFSpeechRecognitionTask?
    var text = "", wakeMode = false, onWake: (() -> Void)?, lastWake = Date.distantPast
    static let wakeWords = ["露米", "路米", "鹿米", "小诊所"]

    func authorized(_ go: @escaping (Bool) -> Void) {
        SFSpeechRecognizer.requestAuthorization { st in DispatchQueue.main.async { go(st == .authorized) } }
    }
    private func begin(handler: @escaping (String, Bool) -> Void) {
        stop()
        let r = SFSpeechAudioBufferRecognitionRequest(); r.shouldReportPartialResults = true
        if rec?.supportsOnDeviceRecognition == true { r.requiresOnDeviceRecognition = true }
        req = r; text = ""
        engine = AVAudioEngine()
        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 2048, format: input.outputFormat(forBus: 0)) { b, _ in r.append(b) }
        engine.prepare(); try? engine.start()
        task = rec?.recognitionTask(with: r) { res, err in
            if let t = res?.bestTranscription.formattedString { handler(t, res?.isFinal ?? false) }
            if err != nil { handler(self.text, true) }
        }
    }
    func stop() {
        engine.inputNode.removeTap(onBus: 0); engine.stop(); req?.endAudio(); task?.cancel(); task = nil; req = nil
    }
    func startPTT(partial: @escaping (String) -> Void) {
        begin { t, _ in self.text = t; DispatchQueue.main.async { partial(t) } }
    }
    func finishPTT(_ done: @escaping (String) -> Void) {
        req?.endAudio()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            let t = self.text; self.stop(); done(t)
            if self.wakeMode { self.startWake() }
        }
    }
    func startWake() {
        wakeMode = true
        begin { t, final in
            if Listener.wakeWords.contains(where: { t.contains($0) }), Date().timeIntervalSince(self.lastWake) > 3 {
                self.lastWake = Date(); DispatchQueue.main.async { self.onWake?() }
            }
            if final { DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { if self.wakeMode { self.startWake() } } }
        }
    }
    func stopWake() { wakeMode = false; stop() }
}

// MARK: - 应用
final class App: NSObject, NSApplicationDelegate, AVSpeechSynthesizerDelegate, NSMenuDelegate {
    let preview = PreviewPanel()
    func menu(_ menu: NSMenu, willHighlight item: NSMenuItem?) {
        guard let a = item?.representedObject as? [String], a.count >= 4, a[0] == "pack" || a[0] == "item" else { preview.hide(); return }
        let id = a[1].addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? a[1]
        preview.show(key: "\(a[0])=\(id)", title: a[3] + (a[2] == "1" ? "  🔒 " + (a.count > 4 ? a[4] : "") : ""), at: NSEvent.mouseLocation)
    }
    func menuDidClose(_ menu: NSMenu) { preview.hide() }
    var panel: NSPanel!, view: Stage!, bubble = Bubble(), listener = Listener()
    lazy var synth: AVSpeechSynthesizer = { let s = AVSpeechSynthesizer(); s.delegate = self; return s }()
    var lastMouse = NSPoint.zero, lastEye = CGPoint.zero, eyeHoldUntil = Date.distantPast
    var players: [String: AVAudioPlayer] = [:], soundGen = 0
    var scale: CGFloat = 0.72, mood = Mood.normal, muted = false, focus = false, sleeping = false
    var open: [Int: [String: Any]] = [:], skinVer = 0, flap: Timer?, hotAt = Date(), hotIsDown = false
    var downAt = Date(), downPt = NSPoint.zero, dragging = false, patted = false, longPress: DispatchWorkItem?, pokeWork: DispatchWorkItem?
    var lastX: CGFloat = 0, online = true
    var downZone: String?
    var feedbackOffsets: [String: Int] = [:]
    var gazeDirection: String?
    let defaults = UserDefaults.standard

    func applicationDidFinishLaunching(_ n: Notification) {
        scale = CGFloat(defaults.double(forKey: "scale") == 0 ? 0.72 : defaults.double(forKey: "scale"))
        let origin = NSPoint(x: defaults.double(forKey: "x") == 0 ? 60 : defaults.double(forKey: "x"),
                             y: defaults.double(forKey: "y") == 0 ? 120 : defaults.double(forKey: "y"))
        panel = NSPanel(contentRect: NSRect(origin: origin, size: NSSize(width: W * scale, height: H * scale)),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating; panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        view = Stage(frame: NSRect(x: 0, y: 0, width: W * scale, height: H * scale))
        view.layout(scale: scale)
        panel.contentView = view
        panel.orderFrontRegardless()
        bubble.onSubmit = { [weak self] t in
            guard let self = self else { return }
            self.bubble.hide()
            self.askClinic(t)
        }
        wireGestures()
        registerHotkey()
        listener.onWake = { [weak self] in self?.wake(voice: true) }
        if defaults.bool(forKey: "wakeword") { listener.authorized { ok in if ok { self.listener.startWake() } } }
        Thread { self.poll() }.start()
        schedule(every: 0.2) { [weak self] in self?.trackEyes() }
        schedule(every: 4) { [weak self] in self?.checkFocusAndIdle() }
        blinkLoop(); idleLoop()
        if CommandLine.arguments.contains("--snapshot") { snapshotAndExit() }
    }

    func schedule(every s: TimeInterval, _ f: @escaping () -> Void) {
        let t = Timer(timeInterval: s, repeats: true) { _ in f() }
        t.tolerance = s * 0.3; RunLoop.main.add(t, forMode: .common)
    }

    // ---- 情绪/表情 ----
    func setMood(_ m: Mood) {
        mood = m
        if m != .normal && gazeDirection != nil { gazeDirection = nil; view.gaze(nil) }
        let face: [Mood: (String, String)] = [.normal: ("eyes_open", "mouth_smile"), .happy: ("eyes_happy", "mouth_cat"),
            .worried: ("eyes_worried", "mouth_worried"), .alert: ("eyes_surprised", "mouth_o"), .sleep: ("eyes_closed", "mouth_smile"),
            .listening: ("eyes_open", "mouth_o"), .thinking: ("eyes_open", "mouth_cat"), .dragging: ("eyes_surprised", "mouth_open")]
        let f = face[m]!
        view.setFace(f.0, f.1)
        view.setFloat(speed: m == .sleep ? 0.45 : 1)
        // 思考中：皮肤有“手持全息平板”姿势就用它（与画风一致），否则显示通用全息面板
        if m == .thinking && view.skin.images["pose_think"] != nil { view.holo(false); view.showPose("think", 8) }
        else { view.holo(m == .thinking) }
    }
    /// 常态情绪：严重告警未结束时保持担心；普通警告只在出现后 60 秒内担心，之后恢复常态（告警仍保留在体检与日志里）
    func baseMood() -> Mood {
        if sleeping { return .sleep }
        let now = Date().timeIntervalSince1970
        for e in open.values {
            let sev = e["severity"] as? String ?? "", t = e["t_detect"] as? Double ?? 0
            if sev == "crit" || (sev == "warn" && now - t < 60) { return .worried }
        }
        return .normal
    }
    func settle(after s: Double) {
        DispatchQueue.main.asyncAfter(deadline: .now() + s) { [weak self] in
            guard let self = self, !self.dragging, self.mood != .listening, self.mood != .thinking else { return }
            self.setMood(self.baseMood())
        }
    }
    func refreshLED() {
        let sev = open.values.map { $0["severity"] as? String ?? "" }
        view.ledColor(!online ? .systemGray : sev.contains("crit") ? .systemRed : sev.contains("warn") ? .systemYellow : .systemGreen)
    }

    func say(_ text: String, voice: Bool = false, urgent: Bool = false, hold: TimeInterval = 4.5) {
        if focus && !urgent { return }  // 勿扰：只保留指示灯
        bubble.show(text, near: panel.frame, hold: hold)
        if voice && view.skin.tts && !muted && (!focus || urgent) {  // 默认纯文字；皮肤开启 tts 才朗读
            synth.stopSpeaking(at: .immediate)
            let u = AVSpeechUtterance(string: text)
            u.voice = AVSpeechSynthesisVoice(language: "zh-CN"); u.pitchMultiplier = 1.25; u.rate = 0.52
            synth.speak(u)
        }
    }
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didStart u: AVSpeechUtterance) {
        var open = false
        flap = Timer.scheduledTimer(withTimeInterval: 0.13, repeats: true) { [weak self] _ in
            open.toggle(); self?.view.setFace(nil, open ? "mouth_open" : "mouth_smile")
        }
    }
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        flap?.invalidate(); flap = nil; setMood(mood)
    }

    // ---- 手势 ----
    func interactionZone(_ e: NSEvent) -> String? {
        let p = view.convert(e.locationInWindow, from: nil)
        let x = p.x / view.bounds.width, y = 1 - p.y / view.bounds.height
        let zones = view.activePose.flatMap { view.skin.poseInteractionZones[$0] } ?? view.skin.interactionZones
        return zones.first { _, b in
            b.count == 4 && x >= b[0] && x <= b[2] && y >= b[1] && y < b[3]
        }?.key
    }

    @discardableResult func feedback(_ key: String, for seconds: Double? = 0.75) -> Bool {
        let choices = (view.skin.interactionVariants[key] ?? []).filter { view.skin.images["pose_" + $0] != nil }
        let name: String
        if choices.isEmpty {
            guard let fallback = view.skin.interactionPoses[key], view.skin.images["pose_" + fallback] != nil else { return false }
            name = fallback
        } else {
            let index = feedbackOffsets[key, default: 0] % choices.count
            name = choices[index]
            feedbackOffsets[key] = (index + 1) % choices.count
        }
        view.showPose(name, seconds)
        if seconds != nil && view.skin.sharedPoses.contains(name) {
            if key == "click_foot" || key == "drag_release" { view.hop(height: 4, dur: 0.32) }
            else { view.rotate(view.stage, [0, 0.055, 0], 0.35, key: "sharedFeedback") }
        }
        return true
    }

    func wireGestures() {
        view.onDown = { [weak self] e in
            guard let self = self else { return }
            self.downAt = Date(); self.downPt = NSEvent.mouseLocation; self.dragging = false; self.patted = false
            self.downZone = self.interactionZone(e)
            self.lastX = self.downPt.x
            let w = DispatchWorkItem { [weak self] in
                guard let self = self else { return }
                if self.feedback("press_hold", for: nil) { self.patted = true }
                else { self.pat() }
            }
            self.longPress = w; DispatchQueue.main.asyncAfter(deadline: .now() + 0.55, execute: w)
        }
        view.onDrag = { [weak self] e in
            guard let self = self else { return }
            let p = NSEvent.mouseLocation
            if !self.dragging && hypot(p.x - self.downPt.x, p.y - self.downPt.y) > 4 {
                self.dragging = true; self.longPress?.cancel(); self.setMood(.dragging)
                if self.view.skin.interactionPoses.isEmpty { self.perform("drag") }
                self.say(self.view.skin.line("drag", "哇啊——"), hold: 1.5)
            }
            guard self.dragging else { return }
            var o = self.panel.frame.origin; o.x += e.deltaX; o.y -= e.deltaY
            self.panel.setFrameOrigin(o)
            let vx = p.x - self.lastX; self.lastX = p.x
            let dx = p.x - self.downPt.x, dy = p.y - self.downPt.y
            let key = abs(dy) > abs(dx) ? "drag_lift" : dx < 0 ? "drag_sleeve_left" : "drag_sleeve_right"
            if !self.feedback(key, for: nil) {
                self.view.dangle(max(-0.45, min(0.45, -vx * 0.03)))
            } else if let name = self.view.skin.interactionPoses[key], self.view.skin.sharedPoses.contains(name) {
                self.view.dangle(abs(dy) > abs(dx) ? 0 : dx < 0 ? 0.12 : -0.12)
            } else { self.view.dangle(0) }
        }
        view.onUp = { [weak self] e in
            guard let self = self else { return }
            self.longPress?.cancel()
            if self.dragging {
                self.dragging = false; self.view.dangle(0); self.setMood(.happy)
                if !self.feedback("drag_release") && !self.perform("drop") { self.view.squash() }
                self.say(self.view.skin.line("drop", "到啦！"), hold: 1.5); self.settle(after: 1.5)
                self.defaults.set(Double(self.panel.frame.minX), forKey: "x"); self.defaults.set(Double(self.panel.frame.minY), forKey: "y")
            } else if self.patted {
                if self.view.skin.interactionPoses["press_hold"] != nil { self.view.clearPose() }
                self.view.blush(0.35); self.settle(after: 1.2)
                self.patted = false
            } else if e.clickCount >= 2 {
                self.pokeWork?.cancel()
                if e.clickCount >= 3 && self.feedback("rapid_taps") { return }
                if self.view.skin.interactionPoses["rapid_taps"] != nil {
                    let w = DispatchWorkItem { [weak self] in self?.toggleMute() }
                    self.pokeWork = w; DispatchQueue.main.asyncAfter(deadline: .now() + NSEvent.doubleClickInterval, execute: w)
                } else { self.toggleMute() }
            } else {
                let zone = self.downZone
                let w = DispatchWorkItem { [weak self] in
                    guard let self = self else { return }
                    if let zone = zone, self.feedback(zone) { self.wakeUp() }
                    else { self.poke() }
                }
                self.pokeWork = w; DispatchQueue.main.asyncAfter(deadline: .now() + NSEvent.doubleClickInterval, execute: w)
            }
        }
        view.onRight = { [weak self] e in self?.menu(e) }
    }

    /// 执行皮肤定义的交互动作（原语组合）；皮肤没定义则返回 false 走内置动作
    @discardableResult func perform(_ key: String) -> Bool {
        guard let steps = view.skin.motions[key]?.randomElement() else { return false }
        let dur = motionDuration(steps)
        for st in steps {
            let p = st.components(separatedBy: ":"), a = Array(p.dropFirst())
            func num(_ i: Int, _ d: Double) -> Double { i < a.count ? Double(a[i]) ?? d : d }
            switch p[0] {
            case "tilt": let r = num(0, 0.12); view.tilt([0, r, r, 0], num(1, 1.0))
            case "wave": view.wave()
            case "hop": view.hop(height: num(0, 16))
            case "squash": view.squash()
            case "jiggle": view.jiggle(); view.boing()
            case "boing": view.boing()
            case "cheer":
                view.rotate(view.L["arm_l"]!, [0, -2.7, -2.4, -2.7, 0], 1.4, key: "cheer")
                view.rotate(view.L["arm_r"]!, [0, 2.7, 2.4, 2.7, 0], 1.4, key: "cheer")
            case "blush": view.blush(1); DispatchQueue.main.asyncAfter(deadline: .now() + num(0, 1.5)) { [weak self] in self?.view.blush(0.35) }
            case "spin": view.spin()
            case "stretch": view.stretch()
            case "rub": view.rub()
            case "side_eye":
                eyeHoldUntil = Date().addingTimeInterval(2); lastMouse = .zero
                view.lookAt(dx: CGFloat(num(0, -4)), dy: 0)
            case "holo":
                view.holo(true)
                DispatchQueue.main.asyncAfter(deadline: .now() + num(0, 2.5)) { [weak self] in self?.view.holo(false) }
            case "particle": view.particle(a.first ?? "✨")
            case "face":
                view.setFace(a.count > 0 && !a[0].isEmpty ? a[0] : nil, a.count > 1 && !a[1].isEmpty ? a[1] : nil)
            case "say": say(view.skin.line(a.first ?? "idle"), hold: 3)
            case "shake": view.shake(num(0, 1.6))
            case "collapse": view.collapse()
            case "pose": view.showPose(a.first ?? "", num(1, 2.0))
            case "sound": playSound(a.first ?? "", for: dur)
            default: break
            }
        }
        if steps.contains(where: { $0.hasPrefix("face") }) { settle(after: 2.2) }  // 表情动作结束后回到常态
        return true
    }

    /// 一组动作的动画时长（音效只播这么久）
    func motionDuration(_ steps: [String]) -> Double {
        let fixed: [String: Double] = ["cheer": 1.4, "boing": 0.8, "blush": 1.5, "wave": 1.8, "hop": 0.45, "squash": 0.35, "jiggle": 0.7, "spin": 0.9,
                                       "stretch": 2.0, "rub": 1.5, "collapse": 2.2, "side_eye": 2.0]
        var d = 0.0
        for st in steps {
            let p = st.components(separatedBy: ":")
            switch p[0] {
            case "pose": d = max(d, p.count > 2 ? Double(p[2]) ?? 2 : 2)
            case "shake", "holo": d = max(d, p.count > 1 ? Double(p[1]) ?? 1.6 : 1.6)
            case "tilt": d = max(d, p.count > 2 ? Double(p[2]) ?? 1 : 1)
            default: d = max(d, fixed[p[0]] ?? 0)
            }
        }
        return d > 0 ? d : 1.5
    }

    /// 音效跟随动作时长：动作结束即暂停并记住位置；再次互动从中断处继续，播完自动回到开头
    func playSound(_ name: String, for dur: Double) {
        if muted { NSLog("[sound] 跳过 %@：静音中（双击可取消）", name); return }
        if focus { NSLog("[sound] 跳过 %@：勿扰模式（前台全屏）", name); return }
        guard let path = view.skin.sounds[name] else { NSLog("[sound] 跳过 %@：皮肤没有该音效 %@", name, view.skin.sounds.description); return }
        guard let pl = players[path] ?? (try? AVAudioPlayer(contentsOf: URL(fileURLWithPath: path))) else { NSLog("[sound] 失败 %@：无法打开 %@", name, path); return }
        NSLog("[sound] 播放 %@ 时长 %.1fs 从 %.1fs 起，音量 %.2f", name, dur, pl.currentTime, pl.volume)
        players[path] = pl
        for (k, o) in players where k != path && o.isPlaying { o.pause() }
        if !pl.isPlaying { pl.play() }
        soundGen += 1
        let g = soundGen
        DispatchQueue.main.asyncAfter(deadline: .now() + dur) { [weak self] in
            if self?.soundGen == g { pl.pause() }
        }
    }

    func poke() {
        wakeUp(); setMood(.happy)
        if !perform("poke") {
            let r = Int.random(in: 0..<3)
            if r == 0 { setMood(.alert); view.hop() } else if r == 1 { view.tilt([0, 0.12, 0], 0.5) } else { view.wave() }
        }
        let status = open.isEmpty ? "" : "\n（有 \(open.count) 个异常待处理，右键可体检）"
        if !bubble.panel.isVisible || status != "" { say(view.skin.line("poke", "在呢！") + status, hold: 3) }
        settle(after: 1.6)
    }
    func pat() {
        patted = true; wakeUp(); setMood(.happy); view.blush(1)
        if !perform("pat") {
            view.tilt([0, -0.1, 0.06, -0.06, 0], 1.2)
            view.particle("💗", dx: -30); DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.view.particle("💗", dx: -10) }
        }
        say(view.skin.line("pat", "嘿嘿…"), voice: true, hold: 2.5)
    }
    func toggleMute() {
        muted.toggle(); setMood(.happy); view.particle(muted ? "🔇" : "🔊", dx: -20)
        say(view.skin.line(muted ? "mute_on" : "mute_off", muted ? "安静模式" : "我回来啦"), hold: 2)
        if muted { synth.stopSpeaking(at: .immediate); players.values.forEach { $0.pause() } }
        settle(after: 1.5)
    }

    func menu(_ e: NSEvent) {
        let m = NSMenu()
        func item(_ t: String, _ sel: Selector, _ on: Bool = false) { let i = NSMenuItem(title: t, action: sel, keyEquivalent: ""); i.target = self; i.state = on ? .on : .off; m.addItem(i) }
        item("🩺 立即体检", #selector(checkup))
        for e in open.values {
            guard let ev = e["evidence"] as? [String: Any], let kind = e["kind"] as? String,
                  let name = (ev["name"] ?? ev["parent"] ?? ev["app"]) as? String else { continue }
            let i = NSMenuItem(title: "🤝 信任「\(name)」的\(e["title"] as? String ?? kind)，不再提醒", action: #selector(trust(_:)), keyEquivalent: "")
            i.target = self; i.representedObject = [name, kind]; m.addItem(i)
        }
        item("💬 提需求…（⌃⌥Space，按住可说话；也可说“换成奶蛙”）", #selector(typeRequest))
        m.addItem(skinMenu())
        if let wm = wardrobeMenu() { m.addItem(wm) }
        item("📋 诊断日志（近 7 天）", #selector(openLog))
        m.addItem(.separator())
        item("🎙 唤醒词「露米」常驻监听", #selector(toggleWake), listener.wakeMode)
        item("🔇 静音（双击切换）", #selector(menuMute), muted)
        for (t, s) in [("小", 0.55), ("中", 0.72), ("大", 0.9)] {
            let i = NSMenuItem(title: "尺寸：\(t)", action: #selector(resize(_:)), keyEquivalent: ""); i.target = self
            i.representedObject = s; i.state = abs(scale - CGFloat(s)) < 0.01 ? .on : .off; m.addItem(i)
        }
        m.addItem(.separator())
        item("退出", #selector(quit))
        NSMenu.popUpContextMenu(m, with: e, for: view)
    }
    /// 换装子菜单：按 group 分组；未解锁显示 🔒 和价格，点击进入兑换
    func skinMenu() -> NSMenuItem {
        let root = NSMenuItem(title: "👗 换装", action: nil, keyEquivalent: ""), sub = NSMenu()
        let r = Net.get("/skins", timeout: 2)
        let cur = r?["current"] as? String
        var groups: [String: NSMenu] = [:]
        for p in r?["packs"] as? [[String: Any]] ?? [] {
            guard let id = p["id"] as? String else { continue }
            let locked = p["locked"] as? Bool ?? false
            let title = (locked ? "🔒 " : "") + (p["name"] as? String ?? id) + (locked ? "  \(p["price"] as? String ?? "")" : "")
            let i = NSMenuItem(title: title, action: #selector(switchSkin(_:)), keyEquivalent: "")
            i.target = self; i.representedObject = ["pack", id, locked ? "1" : "0", p["name"] as? String ?? id, p["price"] as? String ?? ""]
            i.state = id == cur ? .on : .off
            if let g = p["group"] as? String {
                if groups[g] == nil {
                    let gm = NSMenu(); gm.delegate = self; groups[g] = gm
                    let gi = NSMenuItem(title: "🎀 " + g, action: nil, keyEquivalent: ""); gi.submenu = gm; sub.addItem(gi)
                }
                groups[g]!.addItem(i)
            } else { sub.addItem(i) }
        }
        if r == nil { sub.addItem(NSMenuItem(title: "（守护进程未运行）", action: nil, keyEquivalent: "")) }
        sub.addItem(.separator())
        let account = NSMenuItem(title: "👤 门户账户中心…", action: #selector(openAccount), keyEquivalent: ""); account.target = self; sub.addItem(account)
        let sync = NSMenuItem(title: "同步门户权益", action: #selector(syncAccount), keyEquivalent: ""); sync.target = self; sub.addItem(sync)
        sub.delegate = self
        root.submenu = sub
        return root
    }
    /// 模块化衣橱：按槽位列出单件衣物，悬停预览“当前穿搭换上这件”的效果
    func wardrobeMenu() -> NSMenuItem? {
        guard let w = Net.get("/wardrobe", timeout: 2), w["modular"] as? Bool == true else { return nil }
        let root = NSMenuItem(title: "🧥 衣橱（单件搭配）", action: nil, keyEquivalent: ""), sub = NSMenu(); sub.delegate = self
        let items = w["items"] as? [[String: Any]] ?? []
        for slot in w["slots"] as? [[String: Any]] ?? [] {
            let sid = slot["id"] as? String ?? ""
            let head = NSMenuItem(title: "— \(slot["name"] as? String ?? sid) —", action: nil, keyEquivalent: ""); head.isEnabled = false; sub.addItem(head)
            for it in items where it["slot"] as? String == sid {
                let id = it["id"] as? String ?? "", locked = it["locked"] as? Bool ?? false, name = it["name"] as? String ?? id
                let incomplete = (it["complete"] as? Bool) == false ? "（部分动作未适配）" : ""
                let i = NSMenuItem(title: (locked ? "🔒 " : "") + name + incomplete + (locked ? "  \(it["price"] as? String ?? "")" : ""),
                                   action: #selector(wearItem(_:)), keyEquivalent: "")
                i.target = self; i.state = (it["equipped"] as? Bool ?? false) ? .on : .off
                i.representedObject = ["item", id, locked ? "1" : "0", name, it["price"] as? String ?? ""]
                sub.addItem(i)
            }
        }
        root.submenu = sub
        return root
    }
    @objc func wearItem(_ i: NSMenuItem) {
        guard let a = i.representedObject as? [String] else { return }
        if a[2] == "1" { openAccount(); return }
        Net.post("/wardrobe", ["equip": a[1]]) { [weak self] r in
            if r?["ok"] as? Bool != true { self?.say(r?["error"] as? String ?? "换装失败", urgent: true) }
            else { self?.preview.cache.removeAll(); self?.setMood(.happy); self?.view.particle("✨"); self?.settle(after: 1.5) }
        }
    }
    @objc func switchSkin(_ i: NSMenuItem) {
        guard let a = i.representedObject as? [String] else { return }
        if a[2] == "1" { openAccount(); return }
        preview.cache.removeAll()
        Net.post("/skin", ["pack": a[1]]) { [weak self] r in
            if r?["ok"] as? Bool != true { self?.say(r?["error"] as? String ?? "换装失败", urgent: true) }
        }
    }
    @objc func openAccount() {
        let signedIn = Net.get("/account")?["logged_in"] as? Bool == true
        Net.post(signedIn ? "/account/manage" : "/account/login", [:]) { [weak self] r in
            let message = r?["ok"] as? Bool == true ? "已打开门户。兑换后请同步权益。" : "暂时无法打开门户账户中心。"
            self?.say(message, urgent: true)
        }
    }
    @objc func syncAccount() {
        Net.post("/account/refresh", [:]) { [weak self] r in
            self?.say(r?["msg"] as? String ?? "暂时无法同步权益。", urgent: true)
        }
    }
    @objc func openLog() {
        Net.post("/history/report", ["days": 7]) { [weak self] r in
            if let path = r?["path"] as? String { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
            else { self?.say("暂时打不开日志：守护进程未运行", urgent: true) }
        }
    }
    @objc func trust(_ i: NSMenuItem) {
        guard let a = i.representedObject as? [String] else { return }
        Net.post("/trust", ["name": a[0], "kinds": [a[1]]]) { [weak self] _ in
            self?.say("好的，以后不再提醒「\(a[0])」这一项～"); self?.setMood(.happy); self?.settle(after: 1.5)
        }
    }
    @objc func checkup() { askClinic("帮我全面体检一下") }
    @objc func typeRequest() { wake(voice: false, typing: true) }
    @objc func menuMute() { toggleMute() }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func toggleWake() {
        if listener.wakeMode { listener.stopWake(); defaults.set(false, forKey: "wakeword"); say("已关闭唤醒词") }
        else { listener.authorized { ok in
            if ok { self.listener.startWake(); self.defaults.set(true, forKey: "wakeword"); self.say("说「露米」就能叫我～") }
            else { self.say("需要在 系统设置→隐私与安全 中允许麦克风与语音识别") } } }
    }
    @objc func resize(_ i: NSMenuItem) {
        scale = CGFloat(i.representedObject as? Double ?? 0.72); defaults.set(Double(scale), forKey: "scale")
        applyScale()
    }
    /// 按当前皮肤画布与尺寸档位调整窗口（保持底边位置不变）
    func applyScale() {
        let f = panel.frame
        panel.setFrame(NSRect(x: f.minX, y: f.minY, width: W * scale, height: H * scale), display: true)
        view.frame = NSRect(x: 0, y: 0, width: W * scale, height: H * scale); view.layout(scale: scale)
    }

    // ---- 唤醒 / 提需求 ----
    func registerHotkey() {
        var ref: EventHotKeyRef?
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey), EventHotKeyID(signature: 0x434C4E43, id: 1),
                            GetApplicationEventTarget(), 0, &ref)
        var specs = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                     EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
        InstallEventHandler(GetApplicationEventTarget(), { _, ev, _ in
            if GetEventKind(ev) == UInt32(kEventHotKeyPressed) { delegate.hotDown() } else { delegate.hotUp() }
            return noErr
        }, 2, &specs, nil, nil)
    }
    func hotDown() {
        guard !hotIsDown else { return }
        hotIsDown = true; hotAt = Date(); wakeUp(); setMood(.listening)
        // 按住超过 0.35s 才开麦，避免轻按也占用麦克风
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
            guard let self = self, self.hotIsDown else { return }
            self.listener.authorized { ok in
                guard ok, self.hotIsDown else { if !ok { self.say("未授权麦克风/语音识别，可轻按快捷键打字") }; return }
                self.bubble.show(self.view.skin.line("listen", "我在听…"), near: self.panel.frame, hold: 0)
                self.listener.startPTT { t in self.bubble.show("🎙 " + t, near: self.panel.frame, hold: 0) }
            }
        }
    }
    func hotUp() {
        hotIsDown = false
        if Date().timeIntervalSince(hotAt) < 0.35 { wake(voice: false, typing: true); return }
        listener.finishPTT { [weak self] t in
            guard let self = self else { return }
            if t.isEmpty { self.say("没听清，再说一次？"); self.settle(after: 1) } else { self.askClinic(t) }
        }
    }
    func wake(voice: Bool, typing: Bool = false) {
        wakeUp(); setMood(.happy)
        if !perform("wake") { view.wave() }
        let greet = view.skin.line("wake", "我在！要优化什么？")
        if typing { bubble.show(greet, near: panel.frame, typing: true) } else { say(greet, voice: voice) }
        settle(after: 2)
    }
    func askClinic(_ text: String) {
        setMood(.thinking); view.tilt([0, 0.1, 0.1, 0], 1.0)
        bubble.show("收到：「\(text)」\n正在体检…", near: panel.frame, hold: 0)
        Net.post("/ask", ["text": text]) { [weak self] r in
            guard let self = self else { return }
            guard let r = r, let reply = r["reply"] as? String else {
                self.setMood(.worried); self.say("连不上诊所守护进程，请先启动 python3 -m clinic.daemon", urgent: true); return
            }
            let adv = (r["advice"] as? [String] ?? []).map { "• " + $0 }.joined(separator: "\n")
            let det = (r["details"] as? [String] ?? []).joined(separator: "\n")
            let note = r["note"] as? String ?? ""
            self.setMood(adv.isEmpty ? .happy : .worried)
            if adv.isEmpty { self.view.particle("✨") }
            self.say([reply, det, adv, note].filter { !$0.isEmpty }.joined(separator: "\n"), voice: false, urgent: true, hold: 9)
            if !self.muted && self.view.skin.tts { let u = AVSpeechUtterance(string: reply); u.voice = AVSpeechSynthesisVoice(language: "zh-CN"); u.pitchMultiplier = 1.25; u.rate = 0.52; self.synth.speak(u) }
            self.settle(after: 3)
        }
    }

    // ---- 空闲行为 ----
    func blinkLoop() {
        DispatchQueue.main.asyncAfter(deadline: .now() + Double.random(in: 2.5...6)) { [weak self] in
            guard let self = self else { return }
            if (self.view.eyes == "eyes_open" || self.view.eyes == "eyes_worried") && !self.view.skin.whole.contains("eyes_closed") {
                let prev = self.view.eyes
                self.view.setFace("eyes_closed")
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { if self.view.eyes == "eyes_closed" && self.mood != .sleep { self.view.setFace(prev) } }
            }
            self.blinkLoop()
        }
    }
    func idleLoop() {
        DispatchQueue.main.asyncAfter(deadline: .now() + Double.random(in: 14...30)) { [weak self] in
            guard let self = self else { return }
            if self.mood == .normal && !self.focus && !self.sleeping && !self.dragging && !self.perform("idle") {
                switch Int.random(in: 0..<6) {
                case 0: self.view.tilt([0, 0.14, 0.14, -0.14, -0.14, 0], 2.6)                       // 东张西望
                case 1: self.view.wave()                                                             // 挥手
                case 2: self.view.stretch(); self.view.setFace("eyes_closed", "mouth_open"); self.settle(after: 2) // 伸懒腰
                case 3: self.view.holo(true); self.view.setFace(nil, "mouth_cat")                    // 看全息心电图
                    DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.view.holo(false); self.settle(after: 0) }
                case 4: for i in 0..<3 { DispatchQueue.main.asyncAfter(deadline: .now() + Double(i) * 0.6) { self.view.particle("♪", dx: CGFloat(i * 8 - 30)) } }
                    self.view.setFace("eyes_happy", "mouth_cat"); self.settle(after: 2)             // 哼歌
                default: self.view.hop(height: 10, dur: 0.5); self.say(self.view.skin.line("idle", "巡逻中～"), hold: 3)
                }
            }
            self.idleLoop()
        }
    }
    func trackEyes() {
        guard mood != .sleep, !dragging, Date() > eyeHoldUntil else { return }
        let m = NSEvent.mouseLocation
        if !view.skin.gazePoses.isEmpty {
            guard mood == .normal, !patted, view.activePose == nil else { return }
            let f = panel.frame
            let dx = (m.x - f.minX) / f.width - 0.516
            let dy = (m.y - f.minY) / f.height - 0.797
            let direction: String? = f.contains(m) && hypot(dx, dy) > 0.052
                ? (abs(dx) > abs(dy) * 1.3 ? (dx < 0 ? "left" : "right") : (dy < 0 ? "down" : "up")) : nil
            if gazeDirection != direction { gazeDirection = direction; view.gaze(direction) }
            return
        }
        guard view.skin.eyeTrack else { return }
        guard m != lastMouse else { return }  // 鼠标不动就不做任何事
        lastMouse = m
        let c = NSPoint(x: panel.frame.midX, y: panel.frame.midY + 30 * scale), dx = m.x - c.x, dy = m.y - c.y
        let t = hypot(dx, dy) > 700 ? CGPoint.zero :
            CGPoint(x: (max(-3, min(3, dx / 60)) * 2).rounded() / 2, y: (max(-2.5, min(2.5, dy / 60)) * 2).rounded() / 2)
        guard t != lastEye else { return }
        lastEye = t; view.lookAt(dx: t.x, dy: t.y)
    }
    func wakeUp() { if sleeping { sleeping = false; setMood(baseMood()) } }
    func checkFocusAndIdle() {
        if mood == .worried && !dragging && baseMood() == .normal { setMood(.normal) }  // 普通警告 60 秒后恢复常态
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)
        if idle > 300 && !sleeping && mood == .normal { sleeping = true; setMood(.sleep); view.particle("💤") }
        else if idle < 5 && sleeping { wakeUp() }
        if sleeping && Int.random(in: 0..<3) == 0 { view.particle("💤") }
        let f = frontIsFullscreen()
        if f != focus {
            focus = f
            NSAnimationContext.runAnimationGroup { $0.duration = 0.4; panel.animator().alphaValue = f ? 0.35 : 1 }
            if f { bubble.hide() }
        }
    }
    func frontIsFullscreen() -> Bool {
        guard let app = NSWorkspace.shared.frontmostApplication, app.processIdentifier != getpid(),
              let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
        else { return false }
        let sizes = NSScreen.screens.map { $0.frame.size }
        for w in list where (w[kCGWindowOwnerPID as String] as? Int32) == app.processIdentifier && (w[kCGWindowLayer as String] as? Int) == 0 {
            if let b = w[kCGWindowBounds as String] as? [String: CGFloat], let ww = b["Width"], let hh = b["Height"],
               sizes.contains(where: { abs($0.width - ww) < 2 && abs($0.height - hh) < 2 }) { return true }
        }
        return false
    }

    // ---- 事件与皮肤同步（长轮询，空闲时阻塞不耗 CPU）----
    func poll() {
        var since = 0
        while true { autoreleasepool {  // 后台线程无自动释放池，每轮手动回收，避免换皮肤时内存累积
            guard let obj = Net.get("/events?since=\(since)&wait=25", timeout: 35) else {
                DispatchQueue.main.async { if self.online { self.online = false; self.refreshLED(); self.say("等待诊所守护进程启动…", urgent: true) } }
                Thread.sleep(forTimeInterval: 3); return
            }
            since = obj["seq"] as? Int ?? since
            let sv = obj["skin_ver"] as? Int ?? 0
            if sv != skinVer, let sk = Net.get("/skin") {
                let data = SkinData.load(sk)
                skinVer = sv
                DispatchQueue.main.async {
                    let first = self.view.skin.ver == 0
                    self.players.values.forEach { $0.stop() }; self.players.removeAll()
                    self.gazeDirection = nil
                    self.feedbackOffsets.removeAll()
                    self.view.apply(data); self.bubble.accent(data.accent); self.refreshLED()
                    self.applyScale()
                    if !first { self.view.particle("✨"); self.say("换装完成～") }
                }
            }
            let evs = obj["events"] as? [[String: Any]] ?? []
            DispatchQueue.main.async {
                if !self.online { self.online = true; self.refreshLED() }
                for e in evs { self.handle(e) }
            }
        } }
    }
    func handle(_ e: [String: Any]) {
        guard let id = e["id"] as? Int else { return }
        Net.post("/ack", ["id": id])
        let state = e["state"] as? String, sev = e["severity"] as? String ?? "info"
        if state == "open" {
            let isNew = open[id] == nil
            open[id] = e; refreshLED()
            guard isNew, sev != "info" else { return }
            wakeUp()
            let d = (e["diagnosis_llm"] as? [String: Any]) ?? (e["diagnosis"] as? [String: Any]) ?? [:]
            let adv = (d["advice"] as? [String] ?? []).prefix(2).map { "• " + $0 }.joined(separator: "\n")
            let crit = sev == "crit"
            setMood(crit ? .alert : .worried); if crit { view.hop() }; view.particle(crit ? "❗" : "💦")
            say("\(e["title"] ?? "")\n病因：\(d["cause"] ?? "")\n\(adv)\n（常驻程序可右键设为信任）", voice: crit, urgent: crit, hold: 8)
            settle(after: 2.5)
        } else if open.removeValue(forKey: id) != nil {
            refreshLED()
            if !focus { setMood(.happy); if !perform("resolved") { view.particle("✨") }; say(view.skin.line("resolved", "恢复啦！"), hold: 2.5); settle(after: 2) }
        }
    }

    // ---- 验收用：渲染各状态 PNG ----
    func snapshotAndExit() {
        let dir = CommandLine.arguments.last!
        DispatchQueue.global().async {
            while self.view.skin.ver == 0 { Thread.sleep(forTimeInterval: 0.2) }
            DispatchQueue.main.async {
                func capture(_ name: String) {
                    let s = 2, size = CGSize(width: W * self.scale, height: H * self.scale)
                    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width) * s, pixelsHigh: Int(size.height) * s,
                                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
                    let ctx = NSGraphicsContext(bitmapImageRep: rep)!.cgContext
                    ctx.scaleBy(x: CGFloat(s), y: CGFloat(s))
                    CATransaction.flush()
                    self.view.layer!.render(in: ctx)
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
                }
                for (name, m) in [("normal", Mood.normal), ("happy", .happy), ("worried", .worried), ("alert", .alert),
                                  ("sleep", .sleep), ("thinking", .thinking), ("dragging", .dragging)] {
                    self.setMood(m)
                    capture(name)
                }
                self.setMood(.normal)
                self.view.clearPose()
                for key in self.view.skin.interactionPoses.keys.sorted() where self.feedback(key, for: nil) {
                    capture(key)
                    if let variants = self.view.skin.interactionVariants[key] {
                        for index in variants.indices.dropFirst() where self.feedback(key, for: nil) {
                            capture("\(key)_\(index + 1)")
                        }
                    }
                }
                self.view.clearPose()
                if self.view.skin.images["pose_seated_crossed_legs"] != nil {
                    self.view.showPose("seated_crossed_legs", nil)
                    capture("seated_crossed_legs")
                    self.view.clearPose()
                }
                for motion in self.view.skin.motions["idle"] ?? [] {
                    guard let step = motion.first, step.hasPrefix("pose:"),
                          let pose = step.split(separator: ":").dropFirst().first,
                          self.view.skin.images["pose_" + pose] != nil else { continue }
                    self.view.showPose(String(pose), nil)
                    capture("idle_" + pose)
                    self.view.clearPose()
                }
                // 悬停预览面板：按真实菜单路径取预览并渲染
                for (n, key, title) in [("preview_pack", "pack=lumi_fishnet.local", "露米·渔网袜  🔒 ¥14.99 / $2.00"),
                                        ("preview_item", "item=legwear.plain", "基础白丝（默认）")] {
                    self.preview.show(key: key, title: title, at: NSPoint(x: 600, y: 500))
                    if let v = self.preview.panel.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                        v.cacheDisplay(in: v.bounds, to: rep)
                        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(n).png"))
                    }
                }
                self.preview.hide()
                NSApp.terminate(nil)
            }
        }
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
