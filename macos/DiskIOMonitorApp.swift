import Cocoa
import UserNotifications
import WebKit

struct StatusSnapshot {
    let healthy: Bool
    let statusText: String
    let startedText: String
    let totalWrite: String
    let totalRead: String
    let writeRate: String
    let nextLog: String
}

final class StatusPopoverViewController: NSViewController {
    var onShowWindow: (() -> Void)?
    var onHideWindow: (() -> Void)?
    var onRestartSampler: (() -> Void)?
    var onQuit: (() -> Void)?

    private let statusDot = NSView()
    private let statusLabel = NSTextField(labelWithString: "正在连接")
    private let startedLabel = NSTextField(labelWithString: "--")
    private let totalWriteValue = NSTextField(labelWithString: "--")
    private let totalReadValue = NSTextField(labelWithString: "--")
    private let writeRateValue = NSTextField(labelWithString: "--")
    private let nextLogValue = NSTextField(labelWithString: "--")

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 310, height: 278))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.translatesAutoresizingMaskIntoConstraints = false
        root.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        view.addSubview(root)

        let header = NSStackView()
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 8

        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 5
        statusDot.layer?.backgroundColor = NSColor.systemRed.cgColor
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            statusDot.widthAnchor.constraint(equalToConstant: 10),
            statusDot.heightAnchor.constraint(equalToConstant: 10),
        ])

        let titleStack = NSStackView()
        titleStack.orientation = .vertical
        titleStack.alignment = .leading
        titleStack.spacing = 3

        let title = NSTextField(labelWithString: "硬盘读写监控")
        title.font = .systemFont(ofSize: 15, weight: .semibold)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 12)
        titleStack.addArrangedSubview(title)
        titleStack.addArrangedSubview(statusLabel)

        header.addArrangedSubview(statusDot)
        header.addArrangedSubview(titleStack)
        root.addArrangedSubview(header)

        let metrics = NSGridView(views: [
            metricRow("启动后写入", totalWriteValue),
            metricRow("启动后读取", totalReadValue),
            metricRow("当前写速", writeRateValue),
            metricRow("下次日志剩余", nextLogValue),
        ])
        metrics.rowSpacing = 9
        metrics.columnSpacing = 18
        metrics.xPlacement = .fill
        metrics.yPlacement = .fill
        metrics.column(at: 0).xPlacement = .leading
        metrics.column(at: 1).xPlacement = .trailing
        root.addArrangedSubview(metrics)

        startedLabel.textColor = .secondaryLabelColor
        startedLabel.font = .systemFont(ofSize: 12)
        root.addArrangedSubview(startedLabel)

        let separator = NSBox()
        separator.boxType = .separator
        root.addArrangedSubview(separator)

        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.alignment = .centerY
        buttons.spacing = 8
        buttons.distribution = .fillEqually

        let showButton = NSButton(title: "显示窗口", target: self, action: #selector(showWindow))
        let restartButton = NSButton(title: "重启采样器", target: self, action: #selector(restartSampler))
        let quitButton = NSButton(title: "退出", target: self, action: #selector(quitApp))
        buttons.addArrangedSubview(showButton)
        buttons.addArrangedSubview(restartButton)
        buttons.addArrangedSubview(quitButton)
        root.addArrangedSubview(buttons)

        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            root.topAnchor.constraint(equalTo: view.topAnchor),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    func update(snapshot: StatusSnapshot) {
        statusDot.layer?.backgroundColor = snapshot.healthy
            ? NSColor.systemGreen.cgColor
            : NSColor.systemRed.cgColor
        statusLabel.stringValue = snapshot.statusText
        startedLabel.stringValue = "启动时间: \(snapshot.startedText)"
        totalWriteValue.stringValue = snapshot.totalWrite
        totalReadValue.stringValue = snapshot.totalRead
        writeRateValue.stringValue = snapshot.writeRate
        nextLogValue.stringValue = snapshot.nextLog
    }

    private func metricRow(_ title: String, _ value: NSTextField) -> [NSView] {
        let label = NSTextField(labelWithString: title)
        label.textColor = .secondaryLabelColor
        label.font = .systemFont(ofSize: 12)
        value.font = .monospacedDigitSystemFont(ofSize: 14, weight: .semibold)
        value.alignment = .right
        return [label, value]
    }

    @objc private func showWindow() {
        onShowWindow?()
    }

    @objc private func restartSampler() {
        onRestartSampler?()
    }

    @objc private func quitApp() {
        onQuit?()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler, UNUserNotificationCenterDelegate, NSPopoverDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var backendOutputPipe: Pipe?
    private var statusItem: NSStatusItem!
    private var statusPopover: NSPopover!
    private var statusPopoverController: StatusPopoverViewController!
    private var popoverRefreshTimer: Timer?
    private let host = "127.0.0.1"
    private let preferredPort = 8765
    private let fallbackPorts =
        Array(18765...18795) +
        Array(28765...28795) +
        Array(38765...38795) +
        Array(46765...46795)
    private var port = 8765
    private var launchChecks = 0
    private var consecutiveHealthFailures = 0
    private var healthTimer: Timer?
    private var isRestartingBackend = false
    private var currentStatusHealthy = false
    private var lastStatusSnapshot = StatusSnapshot(
        healthy: false,
        statusText: "正在启动",
        startedText: "--",
        totalWrite: "--",
        totalRead: "--",
        writeRate: "--",
        nextLog: "--"
    )
    private var sleepActivity: NSObjectProtocol?
    private var monitorDuringSleep = false
    private var shouldQuit = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        setupNotifications()
        registerPowerNotifications()
        registerAppearanceNotifications()
        buildWindow()
        buildStatusItem()
        startBackend()
        waitForBackend()
        startHealthTimer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if shouldQuit {
            return .terminateNow
        }
        hideWindow()
        return .terminateCancel
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            showWindow()
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        endSleepActivity()
        healthTimer?.invalidate()
        popoverRefreshTimer?.invalidate()
        DistributedNotificationCenter.default().removeObserver(self)
        NotificationCenter.default.removeObserver(self)
        writeShutdownSnapshot()
        backend?.terminate()
        backendOutputPipe?.fileHandleForReading.readabilityHandler = nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if shouldQuit {
            return true
        }
        hideWindow()
        return false
    }

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "chooseLogFolder")
        config.userContentController.add(self, name: "sleepModeChanged")
        config.userContentController.add(self, name: "systemNotification")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "硬盘读写监控"
        window.minSize = NSSize(width: 1040, height: 640)
        window.contentView = webView
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        showLoading()
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusPopoverController = StatusPopoverViewController()
        statusPopoverController.onShowWindow = { [weak self] in
            self?.statusPopover.performClose(nil)
            self?.showWindow()
        }
        statusPopoverController.onRestartSampler = { [weak self] in
            self?.statusPopover.performClose(nil)
            self?.restartBackend(reason: "用户从状态栏浮窗手动重启本地采样器")
        }
        statusPopoverController.onQuit = { [weak self] in
            self?.statusPopover.performClose(nil)
            self?.quitFromMenu()
        }
        statusPopover = NSPopover()
        statusPopover.behavior = .transient
        statusPopover.delegate = self
        statusPopover.contentViewController = statusPopoverController
        statusPopover.contentSize = NSSize(width: 310, height: 278)
        statusPopoverController.update(snapshot: lastStatusSnapshot)

        guard let button = statusItem.button else { return }
        button.image = makeStatusImage(healthy: false, appearance: button.effectiveAppearance)
        button.toolTip = "硬盘读写监控"
        button.action = #selector(statusItemClicked(_:))
        button.target = self
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
    }

    private func statusIconBodyColor(appearance: NSAppearance?) -> NSColor {
        let match = appearance?.bestMatch(from: [.darkAqua, .vibrantDark, .aqua, .vibrantLight])
        if match == .darkAqua || match == .vibrantDark {
            return NSColor.white.withAlphaComponent(0.94)
        }
        return NSColor.black.withAlphaComponent(0.78)
    }

    private func makeStatusImage(healthy: Bool, appearance: NSAppearance?) -> NSImage {
        let size = NSSize(width: 22, height: 18)
        let image = NSImage(size: size)
        image.lockFocus()

        let bodyColor = statusIconBodyColor(appearance: appearance)
        bodyColor.setStroke()
        bodyColor.setFill()

        let driveRect = NSRect(x: 2.5, y: 4.0, width: 13.0, height: 10.0)
        let path = NSBezierPath(roundedRect: driveRect, xRadius: 2.2, yRadius: 2.2)
        path.lineWidth = 1.6
        path.stroke()

        for (index, height) in [3.0, 5.0, 7.0].enumerated() {
            let x = 5.0 + CGFloat(index) * 3.2
            let bar = NSBezierPath(
                roundedRect: NSRect(x: x, y: 6.0, width: 1.7, height: height),
                xRadius: 0.8,
                yRadius: 0.8
            )
            bar.fill()
        }

        let lightColor = healthy ? NSColor.systemGreen : NSColor.systemRed
        lightColor.setFill()
        bodyColor.withAlphaComponent(0.96).setStroke()
        let dot = NSBezierPath(ovalIn: NSRect(x: 14.8, y: 1.8, width: 6.4, height: 6.4))
        dot.fill()
        dot.lineWidth = 1.2
        dot.stroke()

        image.unlockFocus()
        image.isTemplate = false
        return image
    }

    private func updateStatusIndicator(healthy: Bool) {
        currentStatusHealthy = healthy
        guard let button = statusItem.button else { return }
        button.image = makeStatusImage(healthy: healthy, appearance: button.effectiveAppearance)
    }

    @objc private func statusAppearanceChanged() {
        updateStatusIndicator(healthy: currentStatusHealthy)
    }

    @objc private func statusItemClicked(_ sender: Any?) {
        guard let event = NSApp.currentEvent else {
            showWindow()
            return
        }

        if event.type == .rightMouseUp {
            toggleStatusPopover()
            return
        }

        if window.isVisible && NSApp.isActive {
            hideWindow()
        } else {
            showWindow()
        }
    }

    private func toggleStatusPopover() {
        if statusPopover.isShown {
            statusPopover.performClose(nil)
            stopPopoverRefreshTimer()
            return
        }
        statusPopoverController.update(snapshot: lastStatusSnapshot)
        if let button = statusItem.button {
            statusPopover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
        refreshStatusPopoverNow()
        startPopoverRefreshTimer()
    }

    private func startPopoverRefreshTimer() {
        popoverRefreshTimer?.invalidate()
        popoverRefreshTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.refreshStatusPopoverNow()
        }
    }

    private func stopPopoverRefreshTimer() {
        popoverRefreshTimer?.invalidate()
        popoverRefreshTimer = nil
    }

    func popoverDidClose(_ notification: Notification) {
        stopPopoverRefreshTimer()
    }

    @objc private func showWindowFromMenu() {
        showWindow()
    }

    @objc private func hideWindowFromMenu() {
        hideWindow()
    }

    @objc private func quitFromMenu() {
        shouldQuit = true
        NSApp.terminate(nil)
    }

    @objc private func restartSamplerFromMenu() {
        restartBackend(reason: "用户从菜单栏手动重启本地采样器")
    }

    private func showWindow() {
        if window == nil {
            buildWindow()
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func hideWindow() {
        window.orderOut(nil)
    }

    private func startBackend() {
        guard backend == nil else { return }
        guard let resources = Bundle.main.resourcePath else {
            showError("找不到 App 资源目录。")
            return
        }
        selectBackendPort()

        let appPath = "\(resources)/app.py"
        let supportDirectory = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first?.appendingPathComponent("硬盘读写监控", isDirectory: true)
        let logDirectory = supportDirectory?.appendingPathComponent("logs", isDirectory: true)
        let configPath = supportDirectory?.appendingPathComponent("config.json", isDirectory: false)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.currentDirectoryURL = URL(fileURLWithPath: resources)
        process.arguments = [
            appPath,
            "--host", host,
            "--port", String(port),
            "--no-browser"
        ]
        process.environment = [
            "PYTHONUNBUFFERED": "1",
            "DISK_IO_MONITOR_LOG_DIR": logDirectory?.path ?? "\(resources)/logs",
            "DISK_IO_MONITOR_CONFIG_PATH": configPath?.path ?? "\(resources)/config.json",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"
        ]

        let output = Pipe()
        output.fileHandleForReading.readabilityHandler = { handle in
            _ = handle.availableData
        }
        backendOutputPipe = output
        process.standardOutput = output
        process.standardError = output

        do {
            try process.run()
            backend = process
            statusItem.button?.toolTip = "硬盘读写监控（端口 \(port)）"
        } catch {
            showError("后台服务启动失败：\(error.localizedDescription)")
        }
    }

    private func restartBackend(reason: String) {
        guard !isRestartingBackend else { return }
        isRestartingBackend = true
        consecutiveHealthFailures = 0
        applyStatusSnapshot(
            StatusSnapshot(
                healthy: false,
                statusText: "正在重启本地采样器",
                startedText: lastStatusSnapshot.startedText,
                totalWrite: lastStatusSnapshot.totalWrite,
                totalRead: lastStatusSnapshot.totalRead,
                writeRate: lastStatusSnapshot.writeRate,
                nextLog: lastStatusSnapshot.nextLog
            )
        )

        DispatchQueue.main.async {
            if self.window?.isVisible == true {
                self.showLoading(message: "正在重启本地采样器...")
            }
        }

        backend?.terminate()
        backend = nil
        backendOutputPipe?.fileHandleForReading.readabilityHandler = nil
        backendOutputPipe = nil

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            self.launchChecks = 0
            self.startBackend()
            self.waitForBackend()
            self.isRestartingBackend = false
        }
    }

    private func startHealthTimer() {
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.checkBackendHealth()
        }
    }

    private func checkBackendHealth() {
        guard !isRestartingBackend else { return }
        if let backend = backend, !backend.isRunning {
            markBackendUnhealthy()
            return
        }

        var request = URLRequest(url: URL(string: "http://\(host):\(port)/api/state")!)
        request.timeoutInterval = 3

        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self = self else { return }
            let snapshot = self.stateSnapshot(from: data)
            let ok = (response as? HTTPURLResponse)?.statusCode == 200 && snapshot != nil

            DispatchQueue.main.async {
                if ok, let snapshot = snapshot {
                    self.consecutiveHealthFailures = 0
                    self.applyStatusSnapshot(snapshot)
                } else {
                    self.markBackendUnhealthy()
                }
            }
        }.resume()
    }

    private func markBackendUnhealthy() {
        consecutiveHealthFailures += 1
        applyStatusSnapshot(
            StatusSnapshot(
                healthy: false,
                statusText: "本地采样器无响应",
                startedText: lastStatusSnapshot.startedText,
                totalWrite: lastStatusSnapshot.totalWrite,
                totalRead: lastStatusSnapshot.totalRead,
                writeRate: lastStatusSnapshot.writeRate,
                nextLog: lastStatusSnapshot.nextLog
            )
        )
        if consecutiveHealthFailures >= 3 {
            restartBackend(reason: "本地采样器连续无响应，自动重启")
        }
    }

    private func isValidStateResponse(_ data: Data?) -> Bool {
        stateSnapshot(from: data) != nil
    }

    private func applyStatusSnapshot(_ snapshot: StatusSnapshot) {
        lastStatusSnapshot = snapshot
        updateStatusIndicator(healthy: snapshot.healthy)
        statusPopoverController?.update(snapshot: snapshot)
    }

    private func refreshStatusPopoverNow() {
        var request = URLRequest(url: URL(string: "http://\(host):\(port)/api/state")!)
        request.timeoutInterval = 3

        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self = self else { return }
            let snapshot = self.stateSnapshot(from: data)
            DispatchQueue.main.async {
                if
                    (response as? HTTPURLResponse)?.statusCode == 200,
                    let snapshot = snapshot
                {
                    self.applyStatusSnapshot(snapshot)
                } else {
                    self.applyStatusSnapshot(
                        StatusSnapshot(
                            healthy: false,
                            statusText: "本地采样器无响应",
                            startedText: self.lastStatusSnapshot.startedText,
                            totalWrite: self.lastStatusSnapshot.totalWrite,
                            totalRead: self.lastStatusSnapshot.totalRead,
                            writeRate: self.lastStatusSnapshot.writeRate,
                            nextLog: self.lastStatusSnapshot.nextLog
                        )
                    )
                }
            }
        }.resume()
    }

    private func stateSnapshot(from data: Data?) -> StatusSnapshot? {
        guard
            let data = data,
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            json["started_at"] != nil,
            let totals = json["totals"] as? [String: Any],
            json["rows"] != nil
        else { return nil }

        let rawStatus = stringValue(json["status"]) ?? "unknown"
        let error = stringValue(json["error"]) ?? ""
        let healthy = rawStatus == "running" && error.isEmpty
        let statusText: String
        if healthy {
            statusText = "运行中"
        } else if !error.isEmpty {
            statusText = error
        } else {
            statusText = rawStatus
        }

        let logEnabled = boolValue(json["log_enabled"])
        let nextLog = logEnabled
            ? formatDuration(doubleValue(json["next_log_in_seconds"]) ?? 0)
            : "已暂停"

        return StatusSnapshot(
            healthy: healthy,
            statusText: statusText,
            startedText: stringValue(json["started_text"]) ?? "--",
            totalWrite: formatBytes(doubleValue(totals["session_write_bytes"]) ?? 0),
            totalRead: formatBytes(doubleValue(totals["session_read_bytes"]) ?? 0),
            writeRate: "\(formatBytes(doubleValue(totals["write_rate_bps"]) ?? 0))/s",
            nextLog: nextLog
        )
    }

    private func doubleValue(_ value: Any?) -> Double? {
        if let number = value as? NSNumber {
            return number.doubleValue
        }
        if let string = value as? String {
            return Double(string)
        }
        return nil
    }

    private func boolValue(_ value: Any?) -> Bool {
        if let bool = value as? Bool {
            return bool
        }
        if let number = value as? NSNumber {
            return number.boolValue
        }
        return false
    }

    private func stringValue(_ value: Any?) -> String? {
        if let string = value as? String {
            return string
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        return nil
    }

    private func formatBytes(_ value: Double) -> String {
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var size = max(0, value)
        for unit in units {
            if size < 1024 || unit == units.last {
                if unit == "B" {
                    return "\(Int(size.rounded())) \(unit)"
                }
                return String(format: "%.2f %@", size, unit)
            }
            size /= 1024
        }
        return String(format: "%.2f PB", size)
    }

    private func formatDuration(_ seconds: Double) -> String {
        let safe = max(0, Int(seconds.rounded()))
        let hours = safe / 3600
        let minutes = (safe % 3600) / 60
        let secs = safe % 60
        if hours > 0 {
            return "\(hours)小时 \(minutes)分"
        }
        if minutes > 0 {
            return "\(minutes)分 \(secs)秒"
        }
        return "\(secs)秒"
    }

    private func selectBackendPort() {
        let candidates = [preferredPort] + fallbackPorts
        for candidate in candidates {
            cleanStaleBackendListeners(on: candidate)
            if listenerPIDs(on: candidate).isEmpty {
                port = candidate
                return
            }
        }
        port = preferredPort
    }

    private func cleanStaleBackendListeners(on candidatePort: Int) {
        let pids = listenerPIDs(on: candidatePort)
        let currentPID = ProcessInfo.processInfo.processIdentifier
        let backendPID = backend?.processIdentifier ?? -1

        for pid in pids where pid != currentPID && pid != backendPID {
            let command = processCommand(pid: pid)
            guard isDiskMonitorProcess(command, port: candidatePort) else { continue }
            Darwin.kill(pid, SIGTERM)
        }
    }

    private func listenerPIDs(on port: Int) -> [Int32] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-nP", "-tiTCP:\(port)", "-sTCP:LISTEN"]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return []
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let output = String(data: data, encoding: .utf8) ?? ""
        return output
            .split(whereSeparator: \.isNewline)
            .compactMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
    }

    private func processCommand(pid: Int32) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-p", String(pid), "-o", "command="]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return ""
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }

    private func isDiskMonitorProcess(_ command: String, port: Int) -> Bool {
        command.contains("硬盘读写监控.app") ||
            command.contains("HardDiskMonitor") ||
            (command.contains("app.py") && command.contains("--port \(port)"))
    }

    private func waitForBackend() {
        launchChecks += 1
        guard launchChecks <= 100 else {
            restartBackend(reason: "后台服务没有按时响应，自动重启")
            return
        }

        var request = URLRequest(url: URL(string: "http://\(host):\(port)/api/state")!)
        request.timeoutInterval = 1.0

        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                let snapshot = self.stateSnapshot(from: data)
                if
                    let http = response as? HTTPURLResponse,
                    http.statusCode == 200,
                    let snapshot = snapshot
                {
                    self.consecutiveHealthFailures = 0
                    self.applyStatusSnapshot(snapshot)
                    self.refreshSleepModeFromBackend()
                    self.loadInterface()
                } else {
                    Timer.scheduledTimer(withTimeInterval: 0.25, repeats: false) { _ in
                        self.waitForBackend()
                    }
                }
            }
        }.resume()
    }

    private func loadInterface() {
        let url = URL(string: "http://\(host):\(port)/")!
        webView.load(URLRequest(url: url))
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "http" || url.scheme == "https" {
            let isLocal = url.host == host && url.port == port
            if !isLocal {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
        }
        decisionHandler(.allow)
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        if message.name == "chooseLogFolder" {
            chooseLogFolder()
            return
        }
        if message.name == "sleepModeChanged" {
            if
                let body = message.body as? [String: Any],
                let enabled = body["monitorDuringSleep"] as? Bool
            {
                setMonitorDuringSleep(enabled)
            }
            return
        }
        if message.name == "systemNotification" {
            if
                let body = message.body as? [String: Any],
                let title = body["title"] as? String,
                let alertBody = body["body"] as? String
            {
                showSystemNotification(title: title, body: alertBody)
            }
        }
    }

    private func setupNotifications() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func registerAppearanceNotifications() {
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(statusAppearanceChanged),
            name: Notification.Name("AppleInterfaceThemeChangedNotification"),
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(statusAppearanceChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
    }

    private func showSystemNotification(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: "disk-io-alert-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound, .list])
    }

    private func chooseLogFolder() {
        let panel = NSOpenPanel()
        panel.title = "选择日志保存文件夹"
        panel.prompt = "选择"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false

        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }
            guard response == .OK, let url = panel.url else { return }
            self.updateBackendLogDirectory(url.path)
        }
    }

    private func updateBackendLogDirectory(_ path: String) {
        var request = URLRequest(url: URL(string: "http://\(host):\(port)/api/settings")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let payload = ["log_directory": path]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200 && error == nil
            let message: String
            if ok {
                message = ""
            } else if let error = error {
                message = error.localizedDescription
            } else if let data = data, let text = String(data: data, encoding: .utf8), !text.isEmpty {
                message = text
            } else {
                message = "日志文件夹设置失败。"
            }

            DispatchQueue.main.async {
                self?.notifyLogDirectorySelection(ok: ok, path: path, error: message)
            }
        }.resume()
    }

    private func notifyLogDirectorySelection(ok: Bool, path: String, error: String) {
        let payload: [String: Any] = [
            "ok": ok,
            "path": path,
            "error": error
        ]
        guard
            let data = try? JSONSerialization.data(withJSONObject: payload),
            let json = String(data: data, encoding: .utf8)
        else { return }
        webView.evaluateJavaScript("window.afterNativeLogDirectorySelected(\(json));")
    }

    private func registerPowerNotifications() {
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(
            self,
            selector: #selector(systemWillSleep),
            name: NSWorkspace.willSleepNotification,
            object: nil
        )
        center.addObserver(
            self,
            selector: #selector(systemDidWake),
            name: NSWorkspace.didWakeNotification,
            object: nil
        )
    }

    @objc private func systemWillSleep() {
        postBackendEvent(path: "/api/power-event", payload: ["event": "sleep"]) { _ in }
    }

    @objc private func systemDidWake() {
        postBackendEvent(path: "/api/power-event", payload: ["event": "wake"]) { [weak self] _ in
            DispatchQueue.main.async {
                self?.refreshSleepModeFromBackend()
            }
        }
    }

    private func refreshSleepModeFromBackend() {
        var request = URLRequest(url: URL(string: "http://\(host):\(port)/api/state")!)
        request.timeoutInterval = 3

        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard
                let self = self,
                (response as? HTTPURLResponse)?.statusCode == 200,
                let data = data,
                let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let enabled = json["monitor_during_sleep"] as? Bool
            else { return }

            DispatchQueue.main.async {
                self.setMonitorDuringSleep(enabled)
            }
        }.resume()
    }

    private func setMonitorDuringSleep(_ enabled: Bool) {
        monitorDuringSleep = enabled
        if enabled {
            beginSleepActivity()
        } else {
            endSleepActivity()
        }
    }

    private func beginSleepActivity() {
        guard sleepActivity == nil else { return }
        sleepActivity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .idleSystemSleepDisabled],
            reason: "硬盘读写监控正在持续采样磁盘读写"
        )
    }

    private func endSleepActivity() {
        if let activity = sleepActivity {
            ProcessInfo.processInfo.endActivity(activity)
            sleepActivity = nil
        }
    }

    private func postBackendEvent(
        path: String,
        payload: [String: Any],
        completion: @escaping (Bool) -> Void
    ) {
        var request = URLRequest(url: URL(string: "http://\(host):\(port)\(path)")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        URLSession.shared.dataTask(with: request) { _, response, error in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200 && error == nil
            completion(ok)
        }.resume()
    }

    private func writeShutdownSnapshot() {
        guard backend != nil else { return }
        guard let url = URL(string: "http://\(host):\(port)/api/shutdown-snapshot") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 3
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)

        let semaphore = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 3.5)
    }

    private func showLoading(message: String = "正在启动本地监控服务...") {
        webView.loadHTMLString(
            """
            <!doctype html>
            <html lang="zh-CN">
              <meta charset="utf-8">
              <style>
                body {
                  margin: 0;
                  height: 100vh;
                  display: grid;
                  place-items: center;
                  font: 15px -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
                  background: #f5f7f8;
                  color: #182126;
                }
                .box { text-align: center; }
                h1 { margin: 0 0 10px; font-size: 24px; }
                p { margin: 0; color: #66747d; }
              </style>
              <body>
                <div class="box">
                  <h1>硬盘读写监控</h1>
                  <p>\(message)</p>
                </div>
              </body>
            </html>
            """,
            baseURL: nil
        )
    }

    private func showError(_ message: String) {
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")

        webView.loadHTMLString(
            """
            <!doctype html>
            <html lang="zh-CN">
              <meta charset="utf-8">
              <style>
                body {
                  margin: 0;
                  height: 100vh;
                  display: grid;
                  place-items: center;
                  font: 15px -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
                  background: #f5f7f8;
                  color: #182126;
                }
                .box {
                  max-width: 560px;
                  padding: 26px;
                  border: 1px solid #d9e1e5;
                  border-radius: 8px;
                  background: white;
                }
                h1 { margin: 0 0 10px; font-size: 22px; }
                p { margin: 0; color: #a83434; line-height: 1.6; }
              </style>
              <body>
                <div class="box">
                  <h1>启动失败</h1>
                  <p>\(escaped)</p>
                </div>
              </body>
            </html>
            """,
            baseURL: nil
        )
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
