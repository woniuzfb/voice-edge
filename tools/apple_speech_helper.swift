#!/usr/bin/env swift

import Foundation
import Speech
import AVFoundation
import CoreAudio
import os.log

extension AVAudioPCMBuffer {
    /// 为另一个异步 Speech request 创建独立 PCM 存储。
    /// 按 AudioBufferList 的实际 mDataByteSize 复制，兼容交错/非交错及不同 PCM sample type。
    func deepCopyForSpeechRequest() -> AVAudioPCMBuffer? {
        guard let copy = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: frameCapacity
        ) else {
            return nil
        }
        copy.frameLength = frameLength

        let sourceList = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: audioBufferList)
        )
        let destinationList = UnsafeMutableAudioBufferListPointer(
            copy.mutableAudioBufferList
        )
        guard sourceList.count == destinationList.count else { return nil }

        for index in 0..<sourceList.count {
            let source = sourceList[index]
            var destination = destinationList[index]
            guard let sourceData = source.mData,
                  let destinationData = destination.mData,
                  destination.mDataByteSize >= source.mDataByteSize else {
                return nil
            }
            memcpy(destinationData, sourceData, Int(source.mDataByteSize))
            destination.mDataByteSize = source.mDataByteSize
            destinationList[index] = destination
        }
        return copy
    }
}

/// 一条独立的识别流: 一个 locale 对应一个 recognizer + request + task。
/// 多条流共用 SpeechHelper 的同一个 AVAudioEngine input tap(同一路麦克风音频喂给多个
/// 识别器), 用于中英文唤醒词并行识别。这是【附加、被动】通道: 只发带 locale 标签的
/// partial/final, 绝不驱动 stop()/exit()。primary(SpeechHelper 原有单路)行为不变。
final class RecognitionStream {
    let locale: String
    let recognizer: SFSpeechRecognizer?
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    // 每次 arm 递增。旧 task 的延迟 final/error 只能观察自己的 generation，不能污染新流。
    var generation = 0
    // 每个 generation 的音频投递诊断。全部在 SpeechHelper.stateLock 下读写。
    var armedAt: TimeInterval = 0
    var audioBufferCount: UInt64 = 0
    var audioFrameCount: UInt64 = 0
    var firstAudioAt: TimeInterval = 0
    var lastAudioAt: TimeInterval = 0
    var resultCount: UInt64 = 0

    // 重挂去重: 同一时刻只允许一次在途重挂。SFSpeechRecognitionTask 在坏音频下常
    // 先回一个空 isFinal、随后又回一次 error(两次独立 handler 调用), 若各自排一次
    // 重挂就会把一条流裂成两条并发 task。用此标志把"是否已排重挂"收敛为单一真值。
    var rearmScheduled = false
    // 连续错误(非端点)计数; 每次真正拿到结果就清零。用于退避与熄灯。
    var failureCount = 0
    // 熄灯: 连续错误触顶后彻底停掉本 secondary 流(它是可选增强, 失败就放弃, 绝不
    // 拖垮 helper), 直到下一次 start() 重新拉起。
    var disabled = false

    init(locale: String) {
        self.locale = locale
        if locale.lowercased() == "auto" || locale.isEmpty {
            self.recognizer = SFSpeechRecognizer()
        } else {
            self.recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale))
        }
    }
}

final class SegmentJob {
    let id: Int
    let buffers: [AVAudioPCMBuffer]
    let locales: [(String, SFSpeechRecognizer)]
    var localeIndex = 0
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    var bestText = ""

    init(
        id: Int,
        buffers: [AVAudioPCMBuffer],
        locales: [(String, SFSpeechRecognizer)]
    ) {
        self.id = id
        self.buffers = buffers
        self.locales = locales
    }
}

final class SpeechHelper: NSObject {
    // private var audioDiagnosticTimer: DispatchSourceTimer?
    private var audioConfigurationObserver: NSObjectProtocol?

    // private let audioDiagnosticLock = NSLock()
    // private var audioBufferCount: UInt64 = 0
    // private var lastAudioBufferAt: TimeInterval = 0


    private var audioRestartScheduled = false
    private var audioRestartAttempts = 0

    private let maxAudioRestartAttempts = 3

    // secondary(多 locale 被动流)连续错误的熄灯阈值。达到即停掉该流, 不再重挂,
    // 直到下一次 start()。secondary 只是唤醒词并行识别的增强, 失败放弃优于自旋。
    private let maxSecondaryFailures = 5
    // 多 locale 常驻 KWS 中，primary 的无语音端点必须只重挂本流，不能退出 helper。
    private var primaryRearmScheduled = false
    private var primaryFailureCount = 0
    private var primaryTaskGeneration = 0


    private let audioEngine = AVAudioEngine()
    // CoreAudio capture and segment recognition are deliberately separated. The tap only
    // copies PCM and enqueues it; VAD, segmentation and Speech task creation run elsewhere.
    private let segmentQueue = DispatchQueue(label: "com.local.speechhelper.segment")
    private var segmentPreroll: [AVAudioPCMBuffer] = []
    private var segmentAudio: [AVAudioPCMBuffer] = []
    private var segmentPrerollFrames: AVAudioFramePosition = 0
    private var segmentFrames: AVAudioFramePosition = 0
    private var segmentQuietFrames: AVAudioFramePosition = 0
    private let segmentNoiseFloorInitial: Float = 0.003
    private var segmentNoiseFloor: Float = 0.003
    private var segmentSpeaking = false
    private var segmentStartFrames: AVAudioFramePosition = 0
    private var segmentObservedBuffers: UInt64 = 0
    private var segmentLastDiagnosticAt: TimeInterval = 0
    private var segmentSequence = 0
    private var segmentJobs: [Int: SegmentJob] = [:]
    // Apple on-device recognition is serialized deliberately. The next segment is dropped
    // while a two-locale job is in flight rather than creating competing Speech tasks.
    private let maxSegmentJobs = 1
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var speechRecognizer: SFSpeechRecognizer?

    private let localeIdentifier: String

    // 多 locale 并行识别(中英文唤醒词并行)的附加 locale。为空 = 单 locale 模式,
    // 行为与旧版一致(dictation 走此路径, 不受影响)。非空时每个附加 locale 各起一条
    // 被动 RecognitionStream, 只发带 locale 标签的 partial/final, 绝不触发 stop/exit。
    private let secondaryLocaleIdentifiers: [String]
    private var secondaryStreams: [RecognitionStream] = []
    private var multiLocale: Bool { !secondaryLocaleIdentifiers.isEmpty }

    private let stateLock = NSLock()
    private var isStopping = false

    // CoreAudio / Speech teardown state
    private var inputNode: AVAudioInputNode?
    private var tapInstalled = false

    // Prevent duplicate cleanup timers and duplicate RunLoop stops.
    private var cleanupFallbackScheduled = false
    private var finalExitStarted = false
    private var delayedExitScheduled = false
    private var hardExitScheduled = false

    // Whether the current stop request should terminate the helper.
    private var shouldExitAfterStop = false

    // Main-thread confined. Do not read or write this from Speech/audio callbacks.
    private(set) var terminationRequested = false

    private static let debugEnabled =
        ProcessInfo.processInfo.environment["SPEECH_HELPER_DEBUG"] == "1"

    private static let logFormatter = ISO8601DateFormatter()

    // 初始化系统日志
    private let logger = OSLog(subsystem: "com.local.speechhelper", category: "Debug")


    init(localeIdentifier rawLocale: String) {
        // 支持逗号分隔的多 locale(如 "zh-CN,en-US"): 第一个为 primary(驱动生命周期,
        // 单 locale 行为不变), 其余为 secondary 被动流(并行识别, 只发带 locale 标签的
        // partial/final)。不含逗号时 secondary 为空 = 旧行为。
        let parts = rawLocale
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let primaryLocale = parts.first ?? rawLocale
        self.localeIdentifier = primaryLocale
        self.secondaryLocaleIdentifiers = parts.count > 1 ? Array(parts.dropFirst()) : []
        super.init()

        self.debugLog("初始化 SpeechHelper, 主语言: \(primaryLocale), 附加语言: \(self.secondaryLocaleIdentifiers)")

        if primaryLocale.lowercased() == "auto" || primaryLocale.isEmpty {
            self.speechRecognizer = SFSpeechRecognizer()
        } else {
            self.speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: primaryLocale))
        }

        if self.speechRecognizer == nil {
            self.debugLog("警告: SFSpeechRecognizer 初始化返回 nil (可能是语言代码错误或系统不支持)")
        }

        for loc in self.secondaryLocaleIdentifiers {
            self.secondaryStreams.append(RecognitionStream(locale: loc))
        }
    }


    private var useOnDeviceRecognition: Bool {
        ProcessInfo.processInfo.environment["SPEECH_HELPER_ON_DEVICE"] == "1"
    }

    private lazy var contextualStrings: [String] = {
        guard let raw = ProcessInfo.processInfo.environment[
            "SPEECH_HELPER_CONTEXTUAL_STRINGS"
        ], let data = raw.data(using: .utf8),
           let values = try? JSONSerialization.jsonObject(with: data) as? [String]
        else { return [] }
        var seen = Set<String>()
        return values.flatMap { value -> [String] in
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return [] }
            let variants = [
                trimmed,
                trimmed.replacingOccurrences(of: "DeepSeek", with: "Deep Seek"),
                trimmed.lowercased()
            ]
            return variants.filter { seen.insert($0).inserted }
        }
    }()

    private func configureRecognitionRequest(
        _ request: SFSpeechAudioBufferRecognitionRequest,
        recognizer: SFSpeechRecognizer
    ) {
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        if !contextualStrings.isEmpty { request.contextualStrings = contextualStrings }
        if #available(macOS 10.15, *) {
            request.requiresOnDeviceRecognition =
                useOnDeviceRecognition && recognizer.supportsOnDeviceRecognition
        }
    }

    private func lifecycleLog(_ message: String) {
        fputs("[SpeechHelper lifecycle] \(message)\n", stderr)
        fflush(stderr)
    }


    private func shutdownState() -> (
        isStopping: Bool,
        shouldExitAfterStop: Bool
    ) {
        stateLock.lock()
        defer { stateLock.unlock() }

        return (
            isStopping: isStopping,
            shouldExitAfterStop: shouldExitAfterStop
        )
    }


    private func requestForAudioAppend()
        -> SFSpeechAudioBufferRecognitionRequest? {

        stateLock.lock()
        defer { stateLock.unlock() }

        if isStopping {
            return nil
        }

        return recognitionRequest
    }

    // tap 每次取得 request + stream + generation 的一致快照。append 后仅当 generation
    // 仍匹配时累计诊断，避免旧 request 的最后一包污染新 task 统计。
    private func secondaryTargetsForAudioAppend()
        -> [(SFSpeechAudioBufferRecognitionRequest, RecognitionStream, Int)] {
        stateLock.lock()
        defer { stateLock.unlock() }
        if isStopping { return [] }
        return secondaryStreams.compactMap { stream in
            guard let request = stream.request else { return nil }
            return (request, stream, stream.generation)
        }
    }

    private func recordSecondaryAudioAppend(
        stream: RecognitionStream,
        generation: Int,
        frameLength: AVAudioFrameCount
    ) {
        let now = ProcessInfo.processInfo.systemUptime
        stateLock.lock()
        defer { stateLock.unlock() }
        guard stream.generation == generation else { return }
        stream.audioBufferCount += 1
        stream.audioFrameCount += UInt64(frameLength)
        if stream.firstAudioAt == 0 { stream.firstAudioAt = now }
        stream.lastAudioAt = now
    }

    private func secondaryDiagnostic(
        stream: RecognitionStream,
        generation: Int
    ) -> [String: Any] {
        let now = ProcessInfo.processInfo.systemUptime
        stateLock.lock()
        defer { stateLock.unlock() }
        let sameGeneration = stream.generation == generation
        let armedAt = sameGeneration ? stream.armedAt : 0
        let firstAt = sameGeneration ? stream.firstAudioAt : 0
        let lastAt = sameGeneration ? stream.lastAudioAt : 0
        return [
            "generation": generation,
            "audio_buffers": sameGeneration ? stream.audioBufferCount : 0,
            "audio_frames": sameGeneration ? stream.audioFrameCount : 0,
            "results": sameGeneration ? stream.resultCount : 0,
            "task_lifetime": armedAt > 0 ? max(0, now - armedAt) : 0,
            "first_audio_delay": (armedAt > 0 && firstAt > 0) ? max(0, firstAt - armedAt) : -1,
            "last_audio_age": lastAt > 0 ? max(0, now - lastAt) : -1
        ]
    }


    // Emit a snapshot only when primary and every secondary request/task are active.
    // Recognition remains concurrent; this event is a readiness barrier, not a serial decoder.
    private func emitRecognizerReadinessIfComplete(reason: String) {
        precondition(Thread.isMainThread)

        stateLock.lock()
        let stopping = isStopping
        let primaryReady = recognitionRequest != nil && recognitionTask != nil
        let secondaryStates = secondaryStreams.map { stream in
            (
                locale: stream.locale,
                ready: stream.request != nil && stream.task != nil,
                generation: stream.generation
            )
        }
        let primaryGeneration = primaryTaskGeneration
        stateLock.unlock()

        guard !stopping, primaryReady else { return }
        guard secondaryStates.allSatisfy({ $0.ready }) else { return }

        var generations: [String: Int] = [localeIdentifier: primaryGeneration]
        for state in secondaryStates {
            generations[state.locale] = state.generation
        }
        emit([
            "type": "recognizers_ready",
            "locales": [localeIdentifier] + secondaryStates.map { $0.locale },
            "generations": generations,
            "reason": reason
        ])
    }

    private func segmentRMS(_ buffer: AVAudioPCMBuffer) -> Float {
        let count = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        guard count > 0, channels > 0 else { return 0 }
        var sum = 0.0
        var total = 0
        if let data = buffer.floatChannelData {
            for channel in 0..<channels {
                for frame in 0..<count {
                    let value = Double(data[channel][frame])
                    sum += value * value
                }
                total += count
            }
        } else if let data = buffer.int16ChannelData {
            for channel in 0..<channels {
                for frame in 0..<count {
                    let value = Double(data[channel][frame]) / Double(Int16.max)
                    sum += value * value
                }
                total += count
            }
        } else {
            return 0
        }
        return total == 0 ? 0 : Float(sqrt(sum / Double(total)))
    }

    private func captureForSegment(_ source: AVAudioPCMBuffer) {
        guard multiLocale, let copy = source.deepCopyForSpeechRequest() else { return }
        segmentQueue.async { [weak self] in self?.consumeSegmentBuffer(copy) }
    }

    private func consumeSegmentBuffer(_ buffer: AVAudioPCMBuffer) {
        let rate = buffer.format.sampleRate
        guard rate > 0 else { return }
        let frames = AVAudioFramePosition(buffer.frameLength)
        let level = segmentRMS(buffer)
        // Wake words are short and often begin softly. Use hysteresis and require a short
        // run of voiced frames instead of one high absolute threshold.
        let startLevel = max(0.0045, segmentNoiseFloor * 2.2)
        let holdLevel = max(0.0030, segmentNoiseFloor * 1.45)
        segmentObservedBuffers += 1
        let now = ProcessInfo.processInfo.systemUptime
        if segmentLastDiagnosticAt == 0 || now - segmentLastDiagnosticAt >= 1.0 {
            segmentLastDiagnosticAt = now
            emit([
                "type": "segment_meter",
                "level": level,
                "noise_floor": segmentNoiseFloor,
                "start_threshold": startLevel,
                "hold_threshold": holdLevel,
                "speaking": segmentSpeaking,
                "observed_buffers": segmentObservedBuffers
            ])
        }
        if !segmentSpeaking {
            segmentNoiseFloor = max(0.0015, segmentNoiseFloor * 0.985 + min(level, 0.03) * 0.015)
            segmentPreroll.append(buffer)
            segmentPrerollFrames += frames
            let limit = AVAudioFramePosition(rate * 0.40)
            while segmentPrerollFrames > limit && segmentPreroll.count > 1 {
                segmentPrerollFrames -= AVAudioFramePosition(segmentPreroll.removeFirst().frameLength)
            }
            if level >= startLevel {
                segmentStartFrames += frames
            } else {
                segmentStartFrames = 0
            }
            guard segmentStartFrames >= AVAudioFramePosition(rate * 0.045) else { return }
            segmentStartFrames = 0
            segmentSpeaking = true
            emit([
                "type": "segment_speech_started",
                "level": level,
                "noise_floor": segmentNoiseFloor,
                "preroll_duration": Double(segmentPrerollFrames) / rate
            ])
            segmentAudio = segmentPreroll
            segmentFrames = segmentPrerollFrames
            segmentQuietFrames = 0
            segmentPreroll.removeAll(keepingCapacity: true)
            segmentPrerollFrames = 0
            return
        }
        segmentAudio.append(buffer)
        segmentFrames += frames
        segmentQuietFrames = level >= holdLevel ? 0 : segmentQuietFrames + frames
        let ended = segmentQuietFrames >= AVAudioFramePosition(rate * 0.45)
        let full = segmentFrames >= AVAudioFramePosition(rate * 3.5)
        guard ended || full else { return }
        let audio = segmentAudio
        let frameCount = segmentFrames
        segmentAudio = []
        segmentFrames = 0
        segmentQuietFrames = 0
        segmentSpeaking = false
        emit([
            "type": "segment_speech_ended",
            "duration": Double(frameCount) / rate,
            "reason": full ? "maximum" : "silence"
        ])
        segmentPreroll = Array(audio.suffix(2))
        segmentPrerollFrames = segmentPreroll.reduce(0) {
            $0 + AVAudioFramePosition($1.frameLength)
        }
        DispatchQueue.main.async { [weak self] in
            self?.recognizeSegment(audio, frames: frameCount, sampleRate: rate)
        }
    }

    private func recognizeSegment(
        _ buffers: [AVAudioPCMBuffer],
        frames: AVAudioFramePosition,
        sampleRate: Double
    ) {
        precondition(Thread.isMainThread)
        guard multiLocale, !shutdownState().isStopping, !buffers.isEmpty else { return }
        guard segmentJobs.count < maxSegmentJobs else {
            emit(["type": "segment_dropped", "reason": "recognizer_busy"])
            return
        }

        var available: [(String, SFSpeechRecognizer)] = []
        if let primary = speechRecognizer, primary.isAvailable {
            available.append((localeIdentifier, primary))
        }
        for stream in secondaryStreams {
            if let recognizer = stream.recognizer, recognizer.isAvailable {
                available.append((stream.locale, recognizer))
            }
        }
        guard !available.isEmpty else { return }

        segmentSequence += 1
        let job = SegmentJob(id: segmentSequence, buffers: buffers, locales: available)
        segmentJobs[job.id] = job
        emit([
            "type": "segment_captured",
            "segment_id": job.id,
            "duration": Double(frames) / sampleRate,
            "locales": available.map { $0.0 },
            "recognition_mode": "sequential"
        ])
        startNextSegmentLocale(job)
    }

    private func startNextSegmentLocale(_ job: SegmentJob) {
        precondition(Thread.isMainThread)
        guard segmentJobs[job.id] === job else { return }
        guard !shutdownState().isStopping else {
            segmentJobs.removeValue(forKey: job.id)
            return
        }
        guard job.localeIndex < job.locales.count else {
            segmentJobs.removeValue(forKey: job.id)
            emit(["type": "segment_complete", "segment_id": job.id])
            return
        }

        let localeIndex = job.localeIndex
        let (locale, recognizer) = job.locales[localeIndex]
        job.bestText = ""
        let request = SFSpeechAudioBufferRecognitionRequest()
        configureRecognitionRequest(request, recognizer: recognizer)
        request.shouldReportPartialResults = true
        job.request = request
        emit([
            "type": "segment_recognizer_started",
            "segment_id": job.id,
            "locale": locale,
            "locale_index": job.localeIndex
        ])

        job.task = recognizer.recognitionTask(with: request) { [weak self, weak job] result, error in
            let recognizedText = result?.bestTranscription.formattedString.trimmingCharacters(
                in: .whitespacesAndNewlines
            ) ?? ""
            let isFinal = result?.isFinal == true
            guard !recognizedText.isEmpty || isFinal || error != nil else { return }
            DispatchQueue.main.async { [weak self, weak job] in
                guard let self = self, let job = job else { return }
                guard self.segmentJobs[job.id] === job else { return }
                guard job.localeIndex == localeIndex else { return }
                if !recognizedText.isEmpty { job.bestText = recognizedText }
                if isFinal || error != nil {
                    self.finishSegmentLocale(job, locale: locale, error: error)
                }
            }
        }

        for buffer in job.buffers {
            if let copy = buffer.deepCopyForSpeechRequest() { request.append(copy) }
        }
        request.endAudio()
    }

    private func finishSegmentLocale(
        _ job: SegmentJob,
        locale: String,
        error: Error?
    ) {
        precondition(Thread.isMainThread)
        guard segmentJobs[job.id] === job else { return }
        let text = job.bestText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            // Preserve the existing Python wake-state machine: one completed segment is exposed
            // as a partial update followed by a final endpoint.
            emit([
                "type": "partial",
                "text": text,
                "locale": locale,
                "source": "segment",
                "segment_id": job.id,
                "locale_index": job.localeIndex
            ])
            emit([
                "type": "final",
                "text": text,
                "locale": locale,
                "source": "segment",
                "segment_id": job.id,
                "locale_index": job.localeIndex
            ])
        } else if let error = error {
            let ns = error as NSError
            emit([
                "type": "segment_error",
                "locale": locale,
                "segment_id": job.id,
                "error": error.localizedDescription,
                "error_code": ns.code
            ])
        }

        job.task = nil
        job.request = nil
        job.localeIndex += 1
        startNextSegmentLocale(job)
    }

    private func clearSegmentState(cancelJobs: Bool) {
        segmentQueue.sync {
            segmentPreroll.removeAll()
            segmentAudio.removeAll()
            segmentPrerollFrames = 0
            segmentFrames = 0
            segmentQuietFrames = 0
            segmentSpeaking = false
            segmentNoiseFloor = segmentNoiseFloorInitial
        }
        guard cancelJobs else { return }
        let jobs = Array(segmentJobs.values)
        segmentJobs.removeAll()
        DispatchQueue.global(qos: .userInitiated).async {
            for job in jobs {
                job.request?.endAudio()
                job.task?.cancel()
            }
        }
    }

    // 专门用于 Debug，输出到 stderr 并写入 macOS 系统日志
    private func debugLog(_ message: String) {
        guard Self.debugEnabled else { return }

        let timestamp = Self.logFormatter.string(from: Date())
        let logMessage = "[\(timestamp)] [DEBUG] \(message)\n"

        // 输出到终端的 stderr
        fputs(logMessage, stderr)
        fflush(stderr)

        // 输出到 macOS Console.app
        //os_log("%{public}s", log: self.logger, type: .debug, message)
    }

    private func emit(_ object: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: object, options: []),
           let line = String(data: data, encoding: .utf8) {
            print(line)
            fflush(stdout)
        }
    }


    private func requestTermination() {
        precondition(Thread.isMainThread)

        terminationRequested = true
        CFRunLoopStop(CFRunLoopGetMain())
    }


    private func failAndTerminate(
        message: String,
        code: Int
    ) {
        if !Thread.isMainThread {
            DispatchQueue.main.async {
                [weak self] in

                self?.failAndTerminate(
                    message: message,
                    code: code
                )
            }

            return
        }

        lifecycleLog(
            "fatal startup error: " +
            "code=\(code), message=\(message)"
        )

        emit([
            "type": "error",
            "error": message,
            "code": code,
        ])

        stateLock.lock()

        isStopping = true
        shouldExitAfterStop = true

        let request = recognitionRequest
        recognitionRequest = nil

        stateLock.unlock()

        if audioEngine.isRunning {
            audioEngine.stop()
        }

        if tapInstalled {
            let node = (
                inputNode
                ?? audioEngine.inputNode
            )

            node.removeTap(
                onBus: 0
            )

            tapInstalled = false
        }

        audioEngine.reset()
        inputNode = nil

        request?.endAudio()

        // Dispatch cancel to background to avoid blocking main thread
        let task = recognitionTask
        DispatchQueue.global(qos: .userInitiated).async {
            task?.cancel()
        }

        requestTermination()
    }


    func requestPermissionsAndStart() {
        self.debugLog("检查语音识别权限...")

        let currentStatus = SFSpeechRecognizer.authorizationStatus()

        switch currentStatus {
        case .authorized:
            self.debugLog("语音识别权限已授权，跳过重复请求")
            requestMicrophonePermissionAndStart()

        case .notDetermined:
            SFSpeechRecognizer.requestAuthorization { [weak self] status in
                DispatchQueue.main.async {
                    guard let self = self else { return }

                    switch status {
                    case .authorized:
                        self.debugLog("权限: 已授权")
                        self.requestMicrophonePermissionAndStart()

                    case .denied:
                        self.failAndTerminate(
                            message: "Speech recognition permission denied",
                            code: 2
                        )

                    case .restricted:
                        self.failAndTerminate(
                            message: "Speech recognition restricted",
                            code: 3
                        )

                    case .notDetermined:
                        self.failAndTerminate(
                            message: "Speech recognition permission not determined",
                            code: 4
                        )

                    @unknown default:
                        self.failAndTerminate(
                            message: "Unknown speech authorization status",
                            code: 5
                        )
                    }
                }
            }

        case .denied:
            failAndTerminate(
                message: "Speech recognition permission denied",
                code: 2
            )

        case .restricted:
            failAndTerminate(
                message: "Speech recognition restricted",
                code: 3
            )

        @unknown default:
            failAndTerminate(
                message: "Unknown speech authorization status",
                code: 5
            )
        }
    }


    /*
     Speech recognition permission and microphone permission are separate TCC
     permissions on macOS.  AVAudioEngine may fail immediately on the first
     run when microphone access has not been requested explicitly.  Asking for
     it before creating the input tap makes that case deterministic and gives
     Python a useful error instead of an apparent recording flash/crash.
     */
    private func requestMicrophonePermissionAndStart() {
        precondition(Thread.isMainThread)

        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            start()

        case .notDetermined:
            self.debugLog("请求麦克风权限...")
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                DispatchQueue.main.async {
                    guard let self = self else { return }

                    if granted {
                        self.start()
                    } else {
                        self.failAndTerminate(
                            message: "Microphone permission denied",
                            code: 8
                        )
                    }
                }
            }

        case .denied, .restricted:
            failAndTerminate(
                message: "Microphone permission denied or restricted",
                code: 8
            )

        @unknown default:
            failAndTerminate(
                message: "Unknown microphone authorization status",
                code: 8
            )
        }
    }

    private func softResetRecognition() {
        precondition(Thread.isMainThread)
        guard multiLocale, !shutdownState().isStopping else { return }
        guard audioEngine.isRunning, tapInstalled else {
            lifecycleLog("soft recognition reset skipped: audio engine is not running")
            return
        }
        clearSegmentState(cancelJobs: true)
        emit([
            "type": "recognition_reset",
            "primary_locale": localeIdentifier,
            "secondary_locales": secondaryLocaleIdentifiers,
            "mode": "segment_only"
        ])
        lifecycleLog("segment collector reset without restarting AVAudioEngine")
    }

    func handleTerminationSignal(_ signalNumber: Int32) {
        switch signalNumber {
        case SIGUSR2:
            lifecycleLog("Received SIGUSR2: soft recognition reset")
            softResetRecognition()

        case SIGUSR1:
            lifecycleLog("Received SIGUSR1: graceful dictation finish")

            stop(
                exitAfterStop: true,
                cancelRecognition: false
            )

        case SIGINT:
            lifecycleLog("Received SIGINT: cancel dictation")

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            // Hard exit safety net: if Speech framework cleanup blocks the
            // main thread for more than 4 seconds, force exit the process.
            scheduleHardExitFallback(timeout: 4.0, reason: "SIGINT")

        case SIGTERM:
            lifecycleLog("Received SIGTERM: cancel dictation")

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            // Hard exit safety net for SIGTERM
            scheduleHardExitFallback(timeout: 4.0, reason: "SIGTERM")

        default:
            lifecycleLog("Received signal \(signalNumber)")

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            scheduleHardExitFallback(timeout: 4.0, reason: "signal \(signalNumber)")
        }
    }


    // private func startAudioDiagnostics() {
    //     precondition(Thread.isMainThread)

    //     audioDiagnosticTimer?.cancel()
    //     audioDiagnosticTimer = nil

    //     audioDiagnosticLock.lock()
    //     audioBufferCount = 0
    //     lastAudioBufferAt = 0
    //     audioDiagnosticLock.unlock()

    //     let timer = DispatchSource.makeTimerSource(queue: .main)

    //     timer.schedule(
    //         deadline: .now() + 0.5,
    //         repeating: 0.5
    //     )

    //     timer.setEventHandler { [weak self] in
    //         guard let self = self else { return }

    //         self.audioDiagnosticLock.lock()
    //         let count = self.audioBufferCount
    //         let lastAt = self.lastAudioBufferAt
    //         self.audioDiagnosticLock.unlock()

    //         let age: String

    //         if lastAt > 0 {
    //             age = String(
    //                 format: "%.2f",
    //                 ProcessInfo.processInfo.systemUptime - lastAt
    //             )
    //         } else {
    //             age = "never"
    //         }

    //         let state = self.shutdownState()

    //         self.lifecycleLog(
    //             "diagnostic: " +
    //             "engineRunning=\(self.audioEngine.isRunning), " +
    //             "tapInstalled=\(self.tapInstalled), " +
    //             "buffers=\(count), " +
    //             "lastBufferAge=\(age)s, " +
    //             "stopping=\(state.isStopping)"
    //         )
    //     }

    //     audioDiagnosticTimer = timer
    //     timer.resume()
    // }


    private func startAudioConfigurationObserver() {
        precondition(Thread.isMainThread)

        if let observer = audioConfigurationObserver {
            NotificationCenter.default.removeObserver(observer)
            audioConfigurationObserver = nil
        }

        audioConfigurationObserver =
            NotificationCenter.default.addObserver(
                forName:
                    .AVAudioEngineConfigurationChange,
                object: audioEngine,
                queue: .main
            ) { [weak self] _ in
                guard let self = self else {
                    return
                }

                let state = self.shutdownState()

                guard !state.isStopping else {
                    self.lifecycleLog(
                        "ignoring audio configuration " +
                        "change during shutdown"
                    )

                    return
                }

                do {
                    let deviceID = (
                        try self
                            .validateDefaultInputHardware()
                    )

                    let node = (
                        self.audioEngine.inputNode
                    )

                    let inputFormat = (
                        node.inputFormat(
                            forBus: 0
                        )
                    )

                    let outputFormat = (
                        node.outputFormat(
                            forBus: 0
                        )
                    )

                    self.lifecycleLog(
                        "AVAudioEngineConfigurationChange: " +
                        "deviceID=\(deviceID), " +
                        "engineRunning=" +
                        "\(self.audioEngine.isRunning), " +
                        "tapInstalled=" +
                        "\(self.tapInstalled), " +
                        "inputSampleRate=" +
                        "\(inputFormat.sampleRate), " +
                        "inputChannels=" +
                        "\(inputFormat.channelCount), " +
                        "outputSampleRate=" +
                        "\(outputFormat.sampleRate), " +
                        "outputChannels=" +
                        "\(outputFormat.channelCount)"
                    )

                    if !self.audioEngine.isRunning {
                        self
                            .scheduleAudioRestartAfterConfigurationChange()
                    }

                } catch {
                    self.lifecycleLog(
                        "audio input unavailable after " +
                        "configuration change: " +
                        error.localizedDescription
                    )

                    self
                        .scheduleAudioRestartAfterConfigurationChange()
                }
            }
    }


    /// Schedule a hard `_exit()` fallback that runs on a background thread.
    /// If the main thread is blocked in Speech framework cleanup (e.g.
    /// `recognitionTask?.cancel()`), the RunLoop-based cleanup timers will
    /// never fire. This ensures the process exits within `timeout` seconds.
    private func scheduleHardExitFallback(timeout: Double, reason: String) {
        guard !hardExitScheduled else { return }
        hardExitScheduled = true

        lifecycleLog(
            "Hard exit fallback scheduled: timeout=\(timeout)s, reason=\(reason)"
        )

        DispatchQueue.global(qos: .background).asyncAfter(
            deadline: .now() + timeout
        ) { [weak self] in
            guard let self = self else { return }

            // Check if the main RunLoop already exited normally.
            if self.terminationRequested && self.finalExitStarted {
                return
            }

            fputs(
                "[SpeechHelper lifecycle] Hard exit fallback triggered: " +
                "reason=\(reason), calling _exit(1)\n",
                stderr
            )
            fflush(stderr)

            // _exit() bypasses atexit handlers and Swift deinit.
            // This is a last resort to avoid being stuck forever.
            _exit(1)
        }
    }


    private func scheduleDelayedCleanupAndExit(reason: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.scheduleDelayedCleanupAndExit(reason: reason)
            }
            return
        }

        guard !finalExitStarted else { return }
        guard !delayedExitScheduled else { return }

        delayedExitScheduled = true

        lifecycleLog(
            "Speech task finished; delaying final process exit: \(reason)"
        )

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self = self else { return }

            self.completeCleanupAndExit(
                reason: "\(reason), after teardown grace period"
            )
        }
    }


    private func currentDefaultInputDeviceID()
        throws -> AudioDeviceID
    {
        precondition(Thread.isMainThread)

        var address = AudioObjectPropertyAddress(
            mSelector:
                kAudioHardwarePropertyDefaultInputDevice,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

        var deviceID = AudioDeviceID(
            kAudioObjectUnknown
        )

        var dataSize = UInt32(
            MemoryLayout<AudioDeviceID>.size
        )

        let status = AudioObjectGetPropertyData(
            AudioObjectID(
                kAudioObjectSystemObject
            ),
            &address,
            0,
            nil,
            &dataSize,
            &deviceID
        )

        guard status == noErr else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: Int(status),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Failed to obtain the default " +
                        "audio input device: OSStatus=\(status)"
                ]
            )
        }

        guard deviceID != kAudioObjectUnknown else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: 11,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "No default microphone input device is available"
                ]
            )
        }

        return deviceID
    }


    private func checkInputDeviceIsAlive(
        _ deviceID: AudioDeviceID
    ) throws {
        precondition(Thread.isMainThread)

        var address = AudioObjectPropertyAddress(
            mSelector:
                kAudioDevicePropertyDeviceIsAlive,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

        var isAlive: UInt32 = 0

        var dataSize = UInt32(
            MemoryLayout<UInt32>.size
        )

        let status = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &dataSize,
            &isAlive
        )

        guard status == noErr else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: Int(status),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Failed to query microphone state: " +
                        "OSStatus=\(status)"
                ]
            )
        }

        guard isAlive != 0 else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: 12,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "The default microphone input device is not active"
                ]
            )
        }
    }


    private func validateDefaultInputHardware()
        throws -> AudioDeviceID
    {
        precondition(Thread.isMainThread)

        let deviceID = try currentDefaultInputDeviceID()

        try checkInputDeviceIsAlive(
            deviceID
        )

        lifecycleLog(
            "default input device validated: " +
            "deviceID=\(deviceID)"
        )

        return deviceID
    }


    private func installInputTap() throws {
        precondition(Thread.isMainThread)

        /*
        Validate the real CoreAudio default input device first.

        AVAudioEngine.inputNode can sometimes return a format object
        even when there is no currently usable hardware input route.
        In that state, installTap() may raise an Objective-C NSException.
        */
        let deviceID = try validateDefaultInputHardware()

        let node = audioEngine.inputNode
        inputNode = node

        if tapInstalled {
            node.removeTap(
                onBus: 0
            )

            tapInstalled = false
        }

        /*
        Log both formats.

        inputFormat and outputFormat can differ during Bluetooth
        profile changes or CoreAudio route transitions.
        */
        let inputFormat = node.inputFormat(
            forBus: 0
        )

        let outputFormat = node.outputFormat(
            forBus: 0
        )

        lifecycleLog(
            "installing input tap: " +
            "deviceID=\(deviceID), " +
            "inputSampleRate=\(inputFormat.sampleRate), " +
            "inputChannels=\(inputFormat.channelCount), " +
            "outputSampleRate=\(outputFormat.sampleRate), " +
            "outputChannels=\(outputFormat.channelCount)"
        )

        guard inputFormat.sampleRate.isFinite,
            inputFormat.sampleRate > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Invalid microphone input sample rate: " +
                        "\(inputFormat.sampleRate)"
                ]
            )
        }

        guard inputFormat.channelCount > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "The microphone has no available input channels"
                ]
            )
        }

        guard outputFormat.sampleRate.isFinite,
            outputFormat.sampleRate > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Invalid microphone output sample rate: " +
                        "\(outputFormat.sampleRate)"
                ]
            )
        }

        guard outputFormat.channelCount > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "The microphone input node has no output channels"
                ]
            )
        }

        /*
        Important:

        Do not force outputFormat here.

        Passing a previously queried AVAudioFormat can fail when
        CoreAudio is switching devices or Bluetooth profiles.

        format: nil tells AVAudioEngine to use the input node's current
        native output format.
        */
        node.installTap(
            onBus: 0,
            bufferSize: 512,
            format: nil
        ) { [weak self] buffer, _ in
            guard let self = self else {
                return
            }

            if self.multiLocale {
                self.captureForSegment(buffer)
                return
            }
            let request = (
                self.requestForAudioAppend()
            )

            request?.append(
                buffer
            )

            // 单变量实验: primary 继续使用 tap 原始 buffer；每条 secondary request
            // 各自获得独立 PCM 存储，排除同一个 AVAudioPCMBuffer 被两个异步 Speech
            // request 共同持有/消费的未公开行为。tap bufferSize 与 recognizer 配置不变。
            if self.multiLocale {
                for (secReq, stream, generation) in self.secondaryTargetsForAudioAppend() {
                    guard let bufferCopy = buffer.deepCopyForSpeechRequest() else {
                        self.emit([
                            "type": "recognizer_state",
                            "locale": stream.locale,
                            "state": "copy_failed",
                            "generation": generation
                        ])
                        continue
                    }
                    secReq.append(bufferCopy)
                    self.recordSecondaryAudioAppend(
                        stream: stream,
                        generation: generation,
                        frameLength: bufferCopy.frameLength
                    )
                }
            }
        }

        tapInstalled = true

        lifecycleLog(
            "input tap installed successfully"
        )
    }


    private func scheduleAudioRestartAfterConfigurationChange() {
        precondition(Thread.isMainThread)

        let state = shutdownState()

        guard !state.isStopping else {
            lifecycleLog(
                "ignoring audio configuration change during shutdown"
            )
            return
        }

        guard !audioRestartScheduled else {
            lifecycleLog("audio restart already scheduled")
            return
        }

        guard audioRestartAttempts < maxAudioRestartAttempts else {
            lifecycleLog(
                "audio restart limit reached"
            )

            emit([
                "type": "error",
                "error":
                    "Audio input repeatedly stopped after device configuration changes"
            ])

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            return
        }

        audioRestartScheduled = true
        audioRestartAttempts += 1

        lifecycleLog(
            "scheduling audio restart after configuration change: " +
            "attempt=\(audioRestartAttempts)"
        )

        /*
        Give CoreAudio time to finish changing the input device/profile.
        Bluetooth devices especially may briefly report an unstable format.
        */
        DispatchQueue.main.asyncAfter(
            deadline: .now() + 0.35
        ) { [weak self] in
            guard let self = self else { return }

            self.audioRestartScheduled = false

            let currentState = self.shutdownState()

            guard !currentState.isStopping else {
                return
            }

            self.restartAudioEngineAfterConfigurationChange()
        }
    }


    private func restartAudioEngineAfterConfigurationChange() {
        precondition(Thread.isMainThread)

        lifecycleLog(
            "rebuilding audio engine after " +
            "configuration change"
        )

        if audioEngine.isRunning {
            audioEngine.stop()
        }

        if tapInstalled {
            let node = (
                inputNode
                ?? audioEngine.inputNode
            )

            node.removeTap(
                onBus: 0
            )

            tapInstalled = false
        }

        inputNode = nil

        /*
        Reset stale graph state before reading the new route.
        */
        audioEngine.reset()

        do {
            _ = try validateDefaultInputHardware()

            /*
            installInputTap() obtains the input node and formats again.
            It does not reuse the old AVAudioFormat.
            */
            try installInputTap()

            audioEngine.prepare()

            try audioEngine.start()

            lifecycleLog(
                "audio engine restart returned: " +
                "running=\(audioEngine.isRunning)"
            )

            if !audioEngine.isRunning {
                scheduleAudioRestartAfterConfigurationChange()
            } else {
                /*
                A successful restart resets the retry counter.
                Otherwise three unrelated configuration changes over
                the lifetime of the helper could exhaust the limit.
                */
                audioRestartAttempts = 0
                // 音频恢复流动 -> 给 secondary 被动流一次干净重挂(多 locale 才有效)。
                // 这样蓝牙断开/切换后, 主流重启、附加语言唤醒识别随之点亮, 而不是在
                // 音频断流期间自旋(旧 bug), 也不是永久熄灯。
                if !multiLocale { rearmSecondaryStreamsAfterAudioRestart() }
            }

        } catch {
            lifecycleLog(
                "audio engine restart failed: " +
                error.localizedDescription
            )

            if tapInstalled {
                let node = (
                    inputNode
                    ?? audioEngine.inputNode
                )

                node.removeTap(
                    onBus: 0
                )

                tapInstalled = false
            }

            inputNode = nil
            audioEngine.reset()

            scheduleAudioRestartAfterConfigurationChange()
        }
    }


    private func startSegmentOnlyMode() {
        precondition(Thread.isMainThread)
        stateLock.lock()
        isStopping = false
        shouldExitAfterStop = false
        recognitionRequest = nil
        recognitionTask = nil
        stateLock.unlock()
        clearSegmentState(cancelJobs: true)

        do {
            try installInputTap()
        } catch {
            failAndTerminate(message: error.localizedDescription, code: 10)
            return
        }
        startAudioConfigurationObserver()
        audioEngine.prepare()
        do {
            try audioEngine.start()
        } catch {
            if tapInstalled, let node = inputNode {
                node.removeTap(onBus: 0)
                tapInstalled = false
            }
            inputNode = nil
            audioEngine.reset()
            failAndTerminate(
                message: "Audio engine failed to start: \(error.localizedDescription)",
                code: 9
            )
            return
        }
        emit([
            "type": "segment_listener_started",
            "preroll_seconds": 0.40,
            "end_silence_seconds": 0.45,
            "maximum_seconds": 3.5,
            "recognition_mode": "segment_only_sequential"
        ])
        emit([
            "type": "started",
            "locale": localeIdentifier,
            "secondary_locales": secondaryLocaleIdentifiers,
            "recognition_mode": "segment_only_sequential"
        ])
        lifecycleLog("segment-only multi-locale listener started")
    }

    func start() {
        precondition(Thread.isMainThread)

        let initialState = shutdownState()

        guard !initialState.isStopping else {
            emit([
                "type": "error",
                "error": "Speech helper is still stopping"
            ])
            return
        }

        guard let recognizer = speechRecognizer else {
            failAndTerminate(
                message: "Failed to create SFSpeechRecognizer",
                code: 6
            )
            return
        }

        guard recognizer.isAvailable else {
            failAndTerminate(
                message: "Speech recognizer is not available",
                code: 7
            )
            return
        }

        /*
        Check the actual CoreAudio input route before creating
        the speech task.
        */
        do {
            _ = try validateDefaultInputHardware()

        } catch {
            failAndTerminate(
                message:
                    "Microphone input is unavailable: " +
                    error.localizedDescription,
                code: 10
            )

            return
        }

        if multiLocale {
            startSegmentOnlyMode()
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        configureRecognitionRequest(request, recognizer: recognizer)
        if #available(macOS 10.15, *) {
            lifecycleLog(
                "on-device recognition: " +
                "\(request.requiresOnDeviceRecognition), " +
                "contextualStrings=\(request.contextualStrings.count)"
            )
        }

        stateLock.lock()
        recognitionRequest = request
        isStopping = false
        shouldExitAfterStop = false
        stateLock.unlock()

        // 1. Create recognition task before starting AVAudioEngine.
        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self = self else { return }

            /*
            Speech callbacks are not guaranteed to run on the main thread.

            During normal recognition:
            - emit partial/final results

            During shutdown:
            - do not emit stale partial text
            - treat the cancellation/error callback as confirmation that
            SFSpeechRecognitionTask has finished unwinding
            */

            if let result = result {
                let text = result.bestTranscription.formattedString

                self.debugLog(
                    "recognition result: " +
                    "final=\(result.isFinal), " +
                    "chars=\(text.count)"
                )

                let state = self.shutdownState()

                if !state.isStopping {
                    var evt: [String: Any] = [
                        "type": result.isFinal ? "final" : "partial",
                        "text": text
                    ]
                    // 单 locale 时不加 locale 字段(与旧输出逐字一致);
                    // 多 locale 时标注 primary locale, 供 Python 区分语言流。
                    if self.multiLocale {
                        evt["locale"] = self.localeIdentifier
                    }
                    self.emit(evt)
                }

                if result.isFinal {
                    DispatchQueue.main.async { [weak self] in
                        guard let self = self else { return }

                        // Read the state again on the main queue because it may have
                        // changed since the Speech callback began.
                        let currentState = self.shutdownState()

                        if currentState.isStopping {
                            if currentState.shouldExitAfterStop {
                                self.scheduleDelayedCleanupAndExit(
                                    reason: "final result during shutdown"
                                )
                            } else {
                                self.completeCleanupWithoutExit(
                                    reason: "final result during shutdown"
                                )
                            }
                        } else if self.multiLocale {
                            self.primaryFailureCount = 0
                            self.schedulePrimaryRearm(backoff: 0.3)
                        } else {
                            // 单 locale / 一次性听写保持原生命周期。
                            self.stop(
                                exitAfterStop: true,
                                cancelRecognition: false
                            )
                        }
                    }

                    return
                }
            }

            if let error = error {
                let nsError = error as NSError

                self.lifecycleLog(
                    "recognition error: " +
                    "domain=\(nsError.domain), " +
                    "code=\(nsError.code), " +
                    "message=\(error.localizedDescription)"
                )

                DispatchQueue.main.async { [weak self] in
                    guard let self = self else { return }

                    // let nsError = error as NSError
                    let state = self.shutdownState()

                    if state.isStopping {
                        self.debugLog(
                            "Recognition task ended during shutdown: " +
                            "\(error.localizedDescription), code=\(nsError.code)"
                        )

                        if state.shouldExitAfterStop {
                            self.scheduleDelayedCleanupAndExit(
                                reason: "recognition cancellation callback"
                            )
                        } else {
                            self.completeCleanupWithoutExit(
                                reason: "recognition cancellation callback"
                            )
                        }

                        return
                    }

                    if self.multiLocale {
                        self.primaryFailureCount += 1
                        let backoff = min(
                            5.0,
                            0.3 * pow(2.0, Double(min(self.primaryFailureCount - 1, 5)))
                        )
                        self.lifecycleLog(
                            "primary[\(self.localeIdentifier)] ended; rearming in " +
                            "\(backoff)s: \(error.localizedDescription), code=\(nsError.code), " +
                            "onDevice=\(self.useOnDeviceRecognition)"
                        )
                        self.schedulePrimaryRearm(backoff: backoff)
                        return
                    }

                    // 单 locale / 一次性听写保持原错误语义。
                    self.debugLog(
                        "Recognition error: \(error.localizedDescription), " +
                        "code=\(nsError.code)"
                    )
                    self.emit([
                        "type": "error",
                        "error": error.localizedDescription
                    ])
                    self.stop(
                        exitAfterStop: true,
                        cancelRecognition: true
                    )
                }
            }
        }

        // 2. Install input tap.
        do {
            try installInputTap()
        } catch {
            failAndTerminate(
                message: error.localizedDescription,
                code: 10
            )
            return
        }

        startAudioConfigurationObserver()

        // 3. Start audio engine last.
        audioEngine.prepare()

        do {
            try audioEngine.start()
        } catch {
            if tapInstalled, let node = inputNode {
                node.removeTap(onBus: 0)
                tapInstalled = false
            }

            stateLock.lock()
            let failedRequest = recognitionRequest
            recognitionRequest = nil
            isStopping = true
            shouldExitAfterStop = true
            stateLock.unlock()

            failedRequest?.endAudio()

            inputNode = nil
            audioEngine.reset()

            emit([
                "type": "error",
                "error": "Audio engine failed to start: \(error.localizedDescription)",
                "code": 9
            ])

            lifecycleLog(
                "audio engine failed to start: \(error.localizedDescription)"
            )

            scheduleCleanupFallback()

            lifecycleLog("about to cancel recognition task after start failure")

            // Dispatch cancel to background to avoid blocking main thread
            let failedTask = recognitionTask
            DispatchQueue.global(qos: .userInitiated).async {
                failedTask?.cancel()
            }

            lifecycleLog("recognition task cancel dispatched after start failure")

            return
        }

        let currentFormat = inputNode?.outputFormat(forBus: 0)

        lifecycleLog(
            "audio engine started: " +
            "running=\(audioEngine.isRunning), " +
            "sampleRate=\(currentFormat?.sampleRate ?? 0), " +
            "channels=\(currentFormat?.channelCount ?? 0)"
        )

        // startAudioDiagnostics()

        if multiLocale {
            emit([
                "type": "segment_listener_started",
                "preroll_seconds": 0.40,
                "end_silence_seconds": 0.45,
                "maximum_seconds": 3.5
            ])
        }
        // The initial primary task was already armed before AVAudioEngine started. Emit it
        // explicitly; previously only secondary streams produced an "armed" JSON event.
        if multiLocale {
            emit([
                "type": "recognizer_state",
                "locale": localeIdentifier,
                "role": "primary",
                "state": "armed",
                "generation": primaryTaskGeneration
            ])
        }

        // 引擎已起、tap 已装、音频在流动 —— 此时并行拉起附加语言的被动识别流。
        startSecondaryStreams()

        emit([
            "type": "started",
            "locale": localeIdentifier,
            "secondary_locales": secondaryLocaleIdentifiers
        ])
    }


    // 多 locale 常驻模式的 primary 自愈：只替换 primary request/task，不碰
    // AVAudioEngine、input tap 或 secondary streams。
    private func schedulePrimaryRearm(backoff: Double) {
        precondition(Thread.isMainThread)
        guard multiLocale, !primaryRearmScheduled else { return }
        guard !shutdownState().isStopping else { return }
        primaryRearmScheduled = true
        let scheduledGeneration = primaryTaskGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + backoff) { [weak self] in
            guard let self = self else { return }
            self.primaryRearmScheduled = false
            guard self.multiLocale else { return }
            guard !self.shutdownState().isStopping else { return }
            guard scheduledGeneration == self.primaryTaskGeneration else { return }
            guard self.secondaryAudioFlowing() else {
                self.lifecycleLog("primary rearm parked: audio is not flowing")
                return
            }
            self.armPrimaryStream()
        }
    }

    private func armPrimaryStream() {
        precondition(Thread.isMainThread)
        guard multiLocale, !shutdownState().isStopping else { return }
        guard let recognizer = speechRecognizer, recognizer.isAvailable else {
            primaryFailureCount += 1
            schedulePrimaryRearm(backoff: min(5.0, Double(primaryFailureCount)))
            return
        }

        primaryTaskGeneration += 1
        let generation = primaryTaskGeneration
        let request = SFSpeechAudioBufferRecognitionRequest()
        configureRecognitionRequest(request, recognizer: recognizer)

        stateLock.lock()
        let staleRequest = recognitionRequest
        let staleTask = recognitionTask
        recognitionRequest = request
        stateLock.unlock()
        staleRequest?.endAudio()
        DispatchQueue.global(qos: .userInitiated).async { staleTask?.cancel() }

        recognitionTask = recognizer.recognitionTask(with: request) {
            [weak self] result, error in
            guard let self = self else { return }
            if let result = result {
                let state = self.shutdownState()
                if !state.isStopping {
                    self.emit([
                        "type": result.isFinal ? "final" : "partial",
                        "text": result.bestTranscription.formattedString,
                        "locale": self.localeIdentifier
                    ])
                }
                if result.isFinal {
                    DispatchQueue.main.async { [weak self] in
                        guard let self = self else { return }
                        guard generation == self.primaryTaskGeneration else { return }
                        guard !self.shutdownState().isStopping else { return }
                        self.primaryFailureCount = 0
                        self.schedulePrimaryRearm(backoff: 0.3)
                    }
                }
                return
            }
            if let error = error {
                let nsError = error as NSError
                DispatchQueue.main.async { [weak self] in
                    guard let self = self else { return }
                    guard generation == self.primaryTaskGeneration else { return }
                    guard !self.shutdownState().isStopping else { return }
                    self.primaryFailureCount += 1
                    let backoff = min(
                        5.0,
                        0.3 * pow(2.0, Double(min(self.primaryFailureCount - 1, 5)))
                    )
                    self.lifecycleLog(
                        "primary[\(self.localeIdentifier)] ended; rearming in " +
                        "\(backoff)s: \(error.localizedDescription), code=\(nsError.code), " +
                        "onDevice=\(self.useOnDeviceRecognition)"
                    )
                    self.schedulePrimaryRearm(backoff: backoff)
                }
            }
        }
        emit([
            "type": "recognizer_state",
            "locale": localeIdentifier,
            "role": "primary",
            "state": "armed",
            "generation": generation
        ])
        emitRecognizerReadinessIfComplete(reason: "primary_rearmed")
        lifecycleLog("primary[\(localeIdentifier)] rearmed: generation=\(generation)")
    }

    // ==== 多 locale 并行识别: 附加(secondary)被动流的创建与自愈 ====
    // 设计红线: secondary 只发带 locale 标签的 partial/final, 绝不调用 stop()/exit,
    // 不触碰 primary 的 recognitionRequest/Task, 因此不影响既有 CoreAudio teardown。
    private func startSecondaryStreams() {
        precondition(Thread.isMainThread)
        guard multiLocale else { return }

        for stream in secondaryStreams {
            // 新会话: 清零上一会话可能遗留的熄灯/失败/在途标志, 从干净状态拉起。
            stream.rearmScheduled = false
            stream.failureCount = 0
            stream.disabled = false
            guard let recognizer = stream.recognizer, recognizer.isAvailable else {
                lifecycleLog("secondary recognizer unavailable: \(stream.locale)")
                emit([
                    "type": "recognizer_state",
                    "locale": stream.locale,
                    "state": "unavailable"
                ])
                continue
            }
            armSecondaryStream(stream, recognizer: recognizer)
        }
    }

    // secondary 流只在音频真在流动时才有意义: 引擎在跑、tap 已装、未在重启、未在关闭。
    // 这是修复"蓝牙关掉即 loop"的关键闸门 —— recognizer.isAvailable 只表示语音识别
    // 服务可用(内置麦恒可用 -> 恒 true), 判断不了"当前有没有真实麦克风音频喂进来",
    // 旧代码用它当唯一闸门, 于是坏音频下每 0.3s 无限重挂自旋。改用真实音频生命周期。
    private func secondaryAudioFlowing() -> Bool {
        precondition(Thread.isMainThread)
        if shutdownState().isStopping { return false }
        if audioRestartScheduled { return false }
        return audioEngine.isRunning && tapInstalled
    }

    // 集中式 secondary 重挂: 去重 + 熄灯 + 退避 + 音频生命周期闸门。所有重挂路径
    // (端点 isFinal、瞬时 error)都只走这里, 保证任一时刻每条流至多一次在途重挂,
    // 消除"空 isFinal + 紧随 error 各排一次 -> 任务翻倍"的堆积。
    private func scheduleSecondaryRearm(
        _ stream: RecognitionStream,
        backoff: Double
    ) {
        precondition(Thread.isMainThread)
        guard multiLocale else { return }
        if stream.rearmScheduled { return }
        if shutdownState().isStopping { return }

        stream.rearmScheduled = true
        DispatchQueue.main.asyncAfter(
            deadline: .now() + backoff
        ) { [weak self, weak stream] in
            guard let self = self, let stream = stream else { return }
            stream.rearmScheduled = false
            guard !self.shutdownState().isStopping else { return }
            // 音频没在流动(路由切换 / 引擎重启中): 不硬重挂自旋。直接放弃这次;
            // 引擎成功重启后由 rearmSecondaryStreamsAfterAudioRestart() 干净重挂。
            guard self.secondaryAudioFlowing() else {
                self.lifecycleLog(
                    "secondary[\(stream.locale)] rearm skipped: audio not flowing"
                )
                return
            }
            guard let r = stream.recognizer, r.isAvailable else { return }
            self.armSecondaryStream(stream, recognizer: r)
        }
    }

    private func armSecondaryStream(
        _ stream: RecognitionStream,
        recognizer: SFSpeechRecognizer
    ) {
        precondition(Thread.isMainThread)
        guard multiLocale else { return }

        // 重挂前先取消上一条 task(放后台, 与 primary 同纪律), 防止旧 task 悬空并发。
        stateLock.lock()
        let staleTask = stream.task
        let staleReq = stream.request
        stream.task = nil
        stream.request = nil
        stateLock.unlock()
        staleReq?.endAudio()
        if let staleTask = staleTask {
            DispatchQueue.global(qos: .userInitiated).async {
                staleTask.cancel()
            }
        }

        let req = SFSpeechAudioBufferRecognitionRequest()
        configureRecognitionRequest(req, recognizer: recognizer)

        stateLock.lock()
        stream.generation += 1
        let generation = stream.generation
        stream.armedAt = ProcessInfo.processInfo.systemUptime
        stream.audioBufferCount = 0
        stream.audioFrameCount = 0
        stream.firstAudioAt = 0
        stream.lastAudioAt = 0
        stream.resultCount = 0
        stream.request = req
        stateLock.unlock()

        let localeTag = stream.locale

        stream.task = recognizer.recognitionTask(with: req) { [weak self, weak stream] result, error in
            guard let self = self, let stream = stream else { return }

            if let result = result {
                let text = result.bestTranscription.formattedString
                self.stateLock.lock()
                if stream.generation == generation { stream.resultCount += 1 }
                self.stateLock.unlock()
                let diagnostic = self.secondaryDiagnostic(
                    stream: stream,
                    generation: generation
                )
                let state = self.shutdownState()
                DispatchQueue.main.async { [weak self, weak stream] in
                    guard let self = self, let stream = stream else { return }
                    guard generation == stream.generation else { return }
                    guard !self.shutdownState().isStopping else { return }
                    // 任意有效 partial/final 都证明本流正常，立即清零连续失败计数。
                    stream.failureCount = 0
                }
                if !state.isStopping {
                    self.emit([
                        "type": result.isFinal ? "final" : "partial",
                        "text": text,
                        "locale": localeTag,
                        "secondary_debug": diagnostic
                    ])
                }

                if result.isFinal {
                    // 端点检测后本流结束; 拿到过结果 -> 清零错误计数, 走集中式重挂
                    // (小退避 + 去重 + 音频闸门), 让唤醒词持续可被该语言识别。
                    DispatchQueue.main.async { [weak self, weak stream] in
                        guard let self = self, let stream = stream else { return }
                        guard generation == stream.generation else { return }
                        stream.failureCount = 0
                        self.scheduleSecondaryRearm(stream, backoff: 0.3)
                    }
                }
                return
            }

            if let error = error {
                let ns = error as NSError
                self.lifecycleLog(
                    "secondary[\(localeTag)] ended: \(error.localizedDescription), code=\(ns.code)"
                )
                DispatchQueue.main.async { [weak self, weak stream] in
                    guard let self = self, let stream = stream else { return }
                    guard generation == stream.generation else { return }
                    guard !self.shutdownState().isStopping else { return }
                    stream.failureCount += 1
                    let exponent = min(stream.failureCount - 1, self.maxSecondaryFailures)
                    let backoff = min(5.0, 0.3 * pow(2.0, Double(exponent)))
                    let diagnostic = self.secondaryDiagnostic(
                        stream: stream,
                        generation: generation
                    )
                    self.emit([
                        "type": "recognizer_state",
                        "locale": localeTag,
                        "state": "backoff",
                        "failures": stream.failureCount,
                        "retry_after": backoff,
                        "error": error.localizedDescription,
                        "error_code": ns.code,
                        "on_device": self.useOnDeviceRecognition,
                        "secondary_debug": diagnostic
                    ])
                    self.scheduleSecondaryRearm(stream, backoff: backoff)
                }
            }
        }
        emit([
            "type": "recognizer_state",
            "locale": localeTag,
            "role": "secondary",
            "state": "armed",
            "generation": generation,
            "secondary_debug": secondaryDiagnostic(
                stream: stream,
                generation: generation
            )
        ])
        emitRecognizerReadinessIfComplete(reason: "secondary_armed")
    }

    // 引擎因配置变化成功重启后, 给 secondary 流一次干净的重挂: 取消陈旧 task/request,
    // 清零 failure/disabled/rearm 标志, 从头拉起。这实现了"路由不稳时暂时熄灯、音频稳定
    // 后再点亮"的语义, 且完全由既有音频生命周期驱动, 不与 primary 的 teardown 相撞。
    private func rearmSecondaryStreamsAfterAudioRestart() {
        precondition(Thread.isMainThread)
        guard multiLocale else { return }
        guard !shutdownState().isStopping else { return }
        for stream in secondaryStreams {
            stateLock.lock()
            let staleTask = stream.task
            let staleReq = stream.request
            stream.task = nil
            stream.request = nil
            stateLock.unlock()
            staleReq?.endAudio()
            if let staleTask = staleTask {
                DispatchQueue.global(qos: .userInitiated).async {
                    staleTask.cancel()
                }
            }
            stream.rearmScheduled = false
            stream.failureCount = 0
            stream.disabled = false
            guard let recognizer = stream.recognizer, recognizer.isAvailable else {
                lifecycleLog("secondary recognizer unavailable on restart: \(stream.locale)")
                continue
            }
            armSecondaryStream(stream, recognizer: recognizer)
        }
    }

    private func scheduleCleanupFallback() {
        precondition(Thread.isMainThread)

        guard !cleanupFallbackScheduled else { return }
        cleanupFallbackScheduled = true

        lifecycleLog("cleanup fallback scheduled for 2.5 seconds")

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            guard let self = self else { return }

            if self.finalExitStarted || self.delayedExitScheduled {
                return
            }

            self.lifecycleLog(
                "cleanup callback timeout; executing fallback"
            )

            let state = self.shutdownState()

            if state.shouldExitAfterStop {
                self.completeCleanupAndExit(
                    reason: "cleanup callback timeout"
                )
            } else {
                self.completeCleanupWithoutExit(
                    reason: "cleanup callback timeout"
                )
            }
        }
    }


    private func completeCleanupAndExit(reason: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.completeCleanupAndExit(reason: reason)
            }
            return
        }

        guard !finalExitStarted else { return }
        finalExitStarted = true

        lifecycleLog("Completing cleanup and exiting: \(reason)")

        /*
        Important:
        Set the termination flag BEFORE releasing Speech objects.

        Releasing recognitionTask can occasionally block while Apple's Speech
        framework is unwinding. The outer RunLoop must already know that shutdown
        was requested.
        */

        /*
        Wake the current RunLoop iteration. The top-level loop checks
        terminationRequested and then exits naturally.
        */
        requestTermination()

        /*
        Do not force all framework objects to deinitialize synchronously here.

        Audio input was already stopped, the tap was removed, and the engine was
        reset in stop(). Allow normal process teardown to release these objects.
        */
        // stateLock.lock()
        // recognitionRequest = nil
        // stateLock.unlock()
        // inputNode = nil

        /*
        Keep recognitionTask referenced until top-level teardown. Assigning nil
        here can synchronously enter Speech framework cleanup and block the main
        thread on some macOS states.
        */
    }


    private func completeCleanupWithoutExit(reason: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.completeCleanupWithoutExit(reason: reason)
            }
            return
        }

        self.debugLog("Completing cleanup without exit: \(reason)")

        stateLock.lock()
        recognitionRequest = nil
        stateLock.unlock()
        recognitionTask = nil
        inputNode = nil

        cleanupFallbackScheduled = false

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            self.stateLock.lock()
            self.shouldExitAfterStop = false
            self.isStopping = false
            self.stateLock.unlock()
        }
    }


    func stop(
        exitAfterStop: Bool,
        cancelRecognition: Bool = true
    ) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.stop(
                    exitAfterStop: exitAfterStop,
                    cancelRecognition: cancelRecognition
                )
            }
            return
        }

        stateLock.lock()

        if isStopping {
            if exitAfterStop {
                shouldExitAfterStop = true
            }

            stateLock.unlock()

            if exitAfterStop {
                scheduleCleanupFallback()
            }

            return
        }

        isStopping = true
        shouldExitAfterStop = exitAfterStop

        let request = recognitionRequest
        recognitionRequest = nil

        stateLock.unlock()

        // audioDiagnosticTimer?.cancel()
        // audioDiagnosticTimer = nil

        lifecycleLog(
            "stop started, exitAfterStop=\(exitAfterStop), " +
            "cancelRecognition=\(cancelRecognition)"
        )

        audioRestartScheduled = false
        if multiLocale { clearSegmentState(cancelJobs: true) }

        if let observer = audioConfigurationObserver {
            NotificationCenter.default.removeObserver(observer)
            audioConfigurationObserver = nil
        }

        /*
        1. Stop audio input.
        */
        if audioEngine.isRunning {
            audioEngine.stop()
        }

        /*
        2. Remove tap exactly once.
        */
        if tapInstalled, let node = inputNode {
            node.removeTap(onBus: 0)
            tapInstalled = false
        }

        /*
        3. Reset AVAudioEngine.
        */
        audioEngine.reset()

        lifecycleLog(
            "audio engine stopped, tap removed, engine reset"
        )

        /*
        4. Tell Speech that audio has ended.
        */
        request?.endAudio()

        // 多 locale: 同步收尾所有 secondary 流(endAudio + 后台 cancel)。
        // 与 primary 同纪律: cancel 放后台线程, 避免 Speech 收尾阻塞主线程。
        if multiLocale {
            stateLock.lock()
            let secReqs = secondaryStreams.map { $0.request }
            let secTasks = secondaryStreams.map { $0.task }
            for stream in secondaryStreams {
                stream.request = nil
                stream.task = nil
            }
            stateLock.unlock()

            for r in secReqs { r?.endAudio() }
            DispatchQueue.global(qos: .userInitiated).async {
                for t in secTasks { t?.cancel() }
            }
        }

        /*
        Schedule fallback BEFORE calling cancel().

        If cancel() or framework teardown unexpectedly blocks, the scheduling
        intent is already recorded. Note that a completely blocked main thread
        still cannot execute the timer, which is why lifecycle logs matter.
        */
        scheduleCleanupFallback()

        // Tell Python that the audio side has stopped.
        emit(["type": "stopped"])
        lifecycleLog("stopped event emitted")

        /*
        5. Request recognition cancellation.
        Dispatch to a background queue because recognitionTask.cancel()
        can block the main thread when the Speech framework is unwinding,
        preventing the RunLoop from processing the exit.
        */
        if cancelRecognition {
            lifecycleLog("about to cancel recognition task (async)")

            let task = recognitionTask
            DispatchQueue.global(qos: .userInitiated).async {
                self.lifecycleLog("cancelling recognition task on background thread")
                task?.cancel()
                self.lifecycleLog("recognition task cancel returned (background)")
            }

        } else {
            /*
            Graceful stop:
            request.endAudio() was already called above.

            Do not cancel the recognition task and do not exit yet.
            Wait for result.isFinal or an error callback.

            scheduleCleanupFallback() is already active, so the helper will still
            exit if Speech never returns a final callback.
            */
            lifecycleLog(
                "graceful recognition finish requested; waiting for final result"
            )
        }
    }
}

var globalHelper: SpeechHelper?
var signalSources: [DispatchSourceSignal] = []

func installSignalHandlers() {
    for signalNumber in [SIGINT, SIGTERM, SIGUSR1, SIGUSR2] {
        signal(signalNumber, SIG_IGN)

        let source = DispatchSource.makeSignalSource(
            signal: signalNumber,
            queue: .main
        )

        source.setEventHandler {
            globalHelper?.handleTerminationSignal(signalNumber)
        }

        source.resume()
        signalSources.append(source)
    }
}

let args = CommandLine.arguments
let locale = args.count >= 2 ? args[1] : "auto"

let helper = SpeechHelper(localeIdentifier: locale)
globalHelper = helper
installSignalHandlers()

helper.requestPermissionsAndStart()

while !helper.terminationRequested {
    autoreleasepool {
        _ = RunLoop.main.run(
            mode: .default,
            before: Date(timeIntervalSinceNow: 0.1)
        )
    }
}

fputs(
    "[SpeechHelper lifecycle] main RunLoop exited; top-level returning\n",
    stderr
)
fflush(stderr)

// Do not explicitly nil globalHelper.
// The audio engine and tap have already been stopped.
// Let process termination reclaim the remaining Speech objects.
